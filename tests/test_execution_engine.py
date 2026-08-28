import asyncio
from dataclasses import replace

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution_broker import SimulatedBroker
from crypto_quant.execution_contracts import (
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    OrderLegType,
    OrderState,
    OrderUpdate,
)
from crypto_quant.execution_engine import (
    ExecutionDispatchError,
    ExecutionEngine,
    ExecutionEngineSettings,
    ExecutionSourceError,
)
from crypto_quant.execution_store import (
    ExecutionDeliveryState,
    ExecutionStore,
    ExecutionUpdateConflict,
)
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskDecisionStatus,
    RiskPolicy,
)
from crypto_quant.risk_store import RiskStore
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
DECISION_TIME = 900_000
NOW = 900_001


def rules() -> MarketRules:
    return MarketRules(
        SYMBOL,
        tick_size=0.1,
        step_size=0.001,
        min_quantity=0.001,
        min_notional=5,
        max_leverage=100,
        brackets=(RiskBracket(0, 1_000_000, 0.005),),
    )


def persist_risk_decision(
    store: RiskStore,
    *,
    index: int = 1,
    published: bool,
):
    intent = StrategyIntent(
        intent_id=f"intent-entry-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=DECISION_TIME + index,
        created_at=DECISION_TIME + index,
        reference_price=100,
        stop_price=99,
        reason="execution_engine_test",
        source_event_id=f"market-entry-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=index,
    )
    account = AccountRiskSnapshot(
        f"account-snapshot-{index}",
        "paper",
        DECISION_TIME + index,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    market = MarketRiskSnapshot(
        f"risk-market-snapshot-{index}",
        SYMBOL,
        DECISION_TIME + index,
        100,
        rules(),
    )
    decision = evaluate_risk(
        RiskContext(
            "risk-primary",
            intent,
            account,
            market,
            RiskPolicy(),
            NOW + index,
        )
    )
    assert decision.status is RiskDecisionStatus.APPROVED
    commit = store.commit_evaluation(decision, updated_at=NOW + index)
    stored = commit.stored_decision.decision
    if published:
        store.mark_published(stored.decision_id, NOW + index)
    return stored


class MarketProvider:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, decision, now_ms):
        self.calls += 1
        return ExecutionMarketSnapshot(
            f"execution-market-{decision.decision_id}-{now_ms}",
            decision.symbol,
            now_ms,
            100,
            99.9,
            100.1,
        )


def make_engine(
    tmp_path,
    risk_store: RiskStore,
    *,
    policy: ExecutionPolicy,
    provider: MarketProvider | None = None,
    broker=None,
    execution_store: ExecutionStore | None = None,
):
    selected_provider = provider or MarketProvider()
    selected_store = execution_store or ExecutionStore(tmp_path / "execution.db")
    selected_broker = broker
    if policy.mode is ExecutionMode.SIMULATION and selected_broker is None:
        selected_broker = SimulatedBroker()
    return (
        ExecutionEngine(
            ExecutionEngineSettings("execution-primary", "paper"),
            policy,
            selected_provider,
            store=selected_store,
            risk_store=risk_store,
            broker=selected_broker,
        ),
        selected_provider,
        selected_store,
    )


def test_shadow_plan_is_persisted_once_without_broker_or_outbox(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=True)
    engine, provider, execution_store = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(),
    )

    first = asyncio.run(engine.plan_decision(decision, now_ms=NOW + 2))
    second = asyncio.run(engine.plan_decision(decision, now_ms=NOW + 3))

    assert first.delivery_state is ExecutionDeliveryState.RECORDED
    assert second.delivery_state is ExecutionDeliveryState.RECORDED
    assert first.command.command_id == second.command.command_id
    assert first.duplicate is False
    assert second.duplicate is True
    assert provider.calls == 1
    assert execution_store.pending_commands() == []
    assert execution_store.metrics()["commands"] == 1
    health = asyncio.run(engine.health_snapshot())
    assert health["network_write_enabled"] is False
    assert health["mode"] == ExecutionMode.SHADOW.value


def test_unpublished_risk_source_is_a_hard_simulation_outbox_barrier(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=False)
    engine, _, execution_store = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
    )

    planned = asyncio.run(engine.plan_decision(decision, now_ms=NOW + 2))
    assert planned.delivery_state is ExecutionDeliveryState.PENDING
    stored = execution_store.load_command(planned.command.command_id)
    assert stored is not None
    assert stored.delivery_state is ExecutionDeliveryState.PENDING
    assert stored.orders[0].state is OrderState.PLANNED

    risk_store.mark_published(decision.decision_id, NOW + 3)
    flushed = asyncio.run(engine.flush_outbox(now_ms=NOW + 3))
    assert flushed.dispatched == 1
    assert flushed.blocked_on_unpublished_source == 0
    stored = execution_store.load_command(planned.command.command_id)
    assert stored is not None
    assert stored.delivery_state is ExecutionDeliveryState.DISPATCHED
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    assert primary.state is OrderState.ACKNOWLEDGED
    assert protective.state is OrderState.PLANNED


def test_risk_input_must_match_the_immutable_risk_store_fact(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=True)
    engine, provider, execution_store = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(),
    )
    tampered = replace(decision, evidence={"tampered": True})

    with pytest.raises(ExecutionSourceError, match="semantics differ from RiskStore"):
        asyncio.run(engine.plan_decision(tampered, now_ms=NOW + 2))
    assert provider.calls == 0
    assert execution_store.metrics()["commands"] == 0


def test_command_is_durable_before_local_broker_dispatch(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=True)
    execution_store = ExecutionStore(tmp_path / "execution.db")

    class AssertingBroker(SimulatedBroker):
        def dispatch(self, command, orders, now_ms):
            stored = execution_store.load_command(command.command_id)
            assert stored is not None
            assert stored.delivery_state is ExecutionDeliveryState.PENDING
            return super().dispatch(command, orders, now_ms)

    engine, _, _ = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        broker=AssertingBroker(),
        execution_store=execution_store,
    )
    result = asyncio.run(engine.plan_decision(decision, now_ms=NOW + 2))
    assert result.delivery_state is ExecutionDeliveryState.DISPATCHED


def test_dispatch_failure_leaves_command_pending_for_restart_recovery(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=True)
    execution_store = ExecutionStore(tmp_path / "execution.db")

    class FailingBroker:
        def dispatch(self, command, orders, now_ms):
            del command, orders, now_ms
            raise RuntimeError("simulated crash")

        def activate_protection(self, command, orders, now_ms):
            del command, orders, now_ms
            return ()

    failed_engine, provider, _ = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        broker=FailingBroker(),
        execution_store=execution_store,
    )
    with pytest.raises(ExecutionDispatchError, match="local simulation dispatch failed"):
        asyncio.run(failed_engine.plan_decision(decision, now_ms=NOW + 2))
    pending = execution_store.pending_commands()
    assert len(pending) == 1
    assert pending[0].orders[0].state is OrderState.PLANNED

    recovered_engine, recovered_provider, _ = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        broker=SimulatedBroker(),
        execution_store=execution_store,
    )
    recovered = asyncio.run(recovered_engine.bootstrap(now_ms=NOW + 3))
    assert recovered.dispatched == 1
    assert recovered.remaining_pending == 0
    assert provider.calls == 1
    assert recovered_provider.calls == 0


def test_concurrent_outbox_flushes_dispatch_one_command_once(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=False)

    class CountingBroker(SimulatedBroker):
        def __init__(self) -> None:
            self.calls = 0

        def dispatch(self, command, orders, now_ms):
            self.calls += 1
            return super().dispatch(command, orders, now_ms)

    broker = CountingBroker()
    engine, _, execution_store = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        broker=broker,
    )
    result = asyncio.run(engine.plan_decision(decision, now_ms=NOW + 2))
    assert result.delivery_state is ExecutionDeliveryState.PENDING
    risk_store.mark_published(decision.decision_id, NOW + 3)

    async def flush_twice():
        return await asyncio.gather(
            engine.flush_outbox(now_ms=NOW + 3),
            engine.flush_outbox(now_ms=NOW + 3),
        )

    asyncio.run(flush_twice())
    assert broker.calls == 1
    assert execution_store.metrics()["dispatched_commands"] == 1


def test_new_external_updates_require_a_dispatched_simulation_command(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=False)
    engine, _, execution_store = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
    )
    planned = asyncio.run(engine.plan_decision(decision, now_ms=NOW + 2))
    stored = execution_store.load_command(planned.command.command_id)
    assert stored is not None
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    update = OrderUpdate(
        "premature-dispatch",
        primary.order_id,
        NOW + 3,
        OrderState.DISPATCHED,
        0,
        0,
        0,
        broker_order_id="sim-premature",
        evidence={"submitted": False, "network_write": False},
    )

    with pytest.raises(ExecutionDispatchError, match="require a dispatched"):
        asyncio.run(engine.record_order_update(update))
    assert execution_store.load_order_update(update.update_id) is None


def test_partial_fill_activates_protection_and_protective_rejection_fails_command(
    tmp_path,
) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=True)
    broker = SimulatedBroker()
    engine, _, execution_store = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        broker=broker,
    )
    planned = asyncio.run(engine.plan_decision(decision, now_ms=NOW + 2))
    stored = execution_store.load_command(planned.command.command_id)
    assert stored is not None
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)

    partial = broker.fill(
        primary,
        fill_quantity=primary.quantity / 2,
        fill_price=100.1,
        fee=0.1,
        event_time=NOW + 3,
    )
    stored = asyncio.run(engine.record_order_update(partial))
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    assert primary.state is OrderState.PARTIALLY_FILLED
    assert protective.state is OrderState.ACKNOWLEDGED
    assert engine.health.protection_activations == 1

    rejected = broker.reject(
        protective,
        event_time=NOW + 4,
        reason="protection_rejected",
    )
    stored = asyncio.run(engine.record_order_update(rejected))
    assert stored.delivery_state is ExecutionDeliveryState.FAILED
    assert stored.failure_reason == "protective_rejected"


def test_entry_command_completes_only_after_protective_fill_closes_exposure(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=True)
    broker = SimulatedBroker()
    engine, _, execution_store = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        broker=broker,
    )
    planned = asyncio.run(engine.plan_decision(decision, now_ms=NOW + 2))
    stored = execution_store.load_command(planned.command.command_id)
    assert stored is not None
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)

    primary_fill = broker.fill(
        primary,
        fill_quantity=primary.quantity,
        fill_price=100.1,
        fee=0.2,
        event_time=NOW + 3,
    )
    stored = asyncio.run(engine.record_order_update(primary_fill))
    assert stored.delivery_state is ExecutionDeliveryState.DISPATCHED
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    assert protective.state is OrderState.ACKNOWLEDGED

    protection_fill = broker.fill(
        protective,
        fill_quantity=protective.quantity,
        fill_price=99,
        fee=0.2,
        event_time=NOW + 4,
    )
    stored = asyncio.run(engine.record_order_update(protection_fill))
    assert stored.delivery_state is ExecutionDeliveryState.COMPLETED
    assert engine.health.commands_completed == 1


def test_protective_fill_cannot_exceed_primary_filled_exposure(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=True)
    broker = SimulatedBroker()
    engine, _, execution_store = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        broker=broker,
    )
    planned = asyncio.run(engine.plan_decision(decision, now_ms=NOW + 2))
    stored = execution_store.load_command(planned.command.command_id)
    assert stored is not None
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    partial = broker.fill(
        primary,
        fill_quantity=primary.quantity / 2,
        fill_price=100.1,
        fee=0.1,
        event_time=NOW + 3,
    )
    stored = asyncio.run(engine.record_order_update(partial))
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    overfill = broker.fill(
        protective,
        fill_quantity=protective.quantity,
        fill_price=99,
        fee=0.1,
        event_time=NOW + 4,
    )

    with pytest.raises(ExecutionUpdateConflict, match="exceeds primary filled exposure"):
        asyncio.run(engine.record_order_update(overfill))
    assert execution_store.load_order_update(overfill.update_id) is None
