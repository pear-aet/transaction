import asyncio

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution_broker import SimulatedBroker
from crypto_quant.execution_contracts import (
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
)
from crypto_quant.execution_engine import (
    ExecutionDispatchError,
    ExecutionEngine,
    ExecutionEngineSettings,
)
from crypto_quant.execution_runtime import ExecutionRuntime, ExecutionRuntimeSettings
from crypto_quant.execution_store import ExecutionDeliveryState, ExecutionStore
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_bus import RiskDecisionBus
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskDecisionStatus,
    RiskPolicy,
)
from crypto_quant.risk_replay import RiskReplayLedger
from crypto_quant.risk_store import RiskStore
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
BASE_TIME = 900_000
NOW = 900_010


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


def persist_decision(store: RiskStore, index: int, *, published: bool):
    decision_time = BASE_TIME + index
    intent = StrategyIntent(
        intent_id=f"intent-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=decision_time,
        created_at=decision_time,
        reference_price=100,
        stop_price=99,
        reason="execution_runtime_test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=100 + index,
    )
    account = AccountRiskSnapshot(
        f"account-{index}",
        "paper",
        decision_time,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    market = MarketRiskSnapshot(
        f"market-{index}",
        SYMBOL,
        decision_time,
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
    stored = store.commit_evaluation(decision, updated_at=NOW + index).stored_decision.decision
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


def engine(
    risk_store: RiskStore,
    execution_store: ExecutionStore,
    provider: MarketProvider,
    *,
    policy: ExecutionPolicy,
    broker=None,
):
    selected_broker = broker
    if policy.mode is ExecutionMode.SIMULATION and selected_broker is None:
        selected_broker = SimulatedBroker()
    return ExecutionEngine(
        ExecutionEngineSettings("execution-primary", "paper"),
        policy,
        provider,
        store=execution_store,
        risk_store=risk_store,
        broker=selected_broker,
    )


async def wait_until(predicate, message: str) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0.002)
    raise AssertionError(message)


def test_runtime_catches_up_published_risk_then_processes_live_bus(tmp_path) -> None:
    async def scenario() -> None:
        risk_store = RiskStore(tmp_path / "risk.db")
        first = persist_decision(risk_store, 1, published=True)
        execution_store = ExecutionStore(tmp_path / "execution.db")
        provider = MarketProvider()
        decision_bus = RiskDecisionBus()
        execution_engine = engine(
            risk_store,
            execution_store,
            provider,
            policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        )
        runtime = ExecutionRuntime(
            decision_bus,
            risk_store,
            execution_engine,
            ExecutionRuntimeSettings(
                "execution-live",
                "execution-upstream-v1",
                outbox_flush_interval_ms=5,
            ),
            clock=lambda: NOW + 20,
        )
        task = asyncio.create_task(runtime.run())

        await wait_until(
            lambda: runtime.health.replayed_decisions == 1,
            "execution runtime did not catch up the published risk decision",
        )
        first_command = execution_store.load_by_source_decision(
            "execution-primary",
            first.decision_id,
        )
        assert first_command is not None
        assert first_command.delivery_state is ExecutionDeliveryState.DISPATCHED
        assert (
            runtime.replay_ledger.offset(
                "execution-upstream-v1",
                runtime._durable_filter(),
            ).sequence
            == first.storage_sequence
        )

        second = persist_decision(risk_store, 2, published=False)
        result = await decision_bus.publish(second, now_ms=NOW + 20)
        assert result.critical_delivered == 1
        risk_store.mark_published(second.decision_id, NOW + 20)

        await wait_until(
            lambda: runtime.health.live_decisions == 1
            and execution_store.metrics()["commands"] == 2
            and execution_store.metrics()["pending_commands"] == 0,
            "execution runtime did not recover the live source-publication race",
        )
        second_command = execution_store.load_by_source_decision(
            "execution-primary",
            second.decision_id,
        )
        assert second_command is not None
        assert second_command.delivery_state is ExecutionDeliveryState.DISPATCHED
        assert provider.calls == 2

        await runtime.stop()
        await asyncio.wait_for(task, timeout=1.0)
        await decision_bus.close()

    asyncio.run(scenario())


def test_periodic_catch_up_releases_a_published_upstream_barrier_without_bus_event(
    tmp_path,
) -> None:
    async def scenario() -> None:
        risk_store = RiskStore(tmp_path / "risk.db")
        first = persist_decision(risk_store, 1, published=False)
        second = persist_decision(risk_store, 2, published=True)
        execution_store = ExecutionStore(tmp_path / "execution.db")
        provider = MarketProvider()
        decision_bus = RiskDecisionBus()
        runtime = ExecutionRuntime(
            decision_bus,
            risk_store,
            engine(
                risk_store,
                execution_store,
                provider,
                policy=ExecutionPolicy(),
            ),
            ExecutionRuntimeSettings(
                "execution-live",
                "execution-upstream-v1",
                outbox_flush_interval_ms=5,
            ),
            clock=lambda: NOW + 20,
        )
        task = asyncio.create_task(runtime.run())
        await wait_until(
            lambda: runtime.health.bootstrap_runs == 1,
            "execution runtime did not bootstrap",
        )
        assert execution_store.metrics()["commands"] == 0

        risk_store.mark_published(first.decision_id, NOW + 20)
        await wait_until(
            lambda: execution_store.metrics()["commands"] == 2,
            "periodic durable catch-up did not release the upstream barrier",
        )
        assert provider.calls == 2
        assert execution_store.load_by_source_decision(
            "execution-primary",
            second.decision_id,
        ) is not None
        assert (
            runtime.replay_ledger.offset(
                "execution-upstream-v1",
                runtime._durable_filter(),
            ).sequence
            == second.storage_sequence
        )

        await runtime.stop()
        await asyncio.wait_for(task, timeout=1.0)
        await decision_bus.close()

    asyncio.run(scenario())


def test_catch_up_commits_upstream_cursor_after_command_persistence_before_dispatch_failure(
    tmp_path,
) -> None:
    async def scenario() -> None:
        risk_store = RiskStore(tmp_path / "risk.db")
        decision = persist_decision(risk_store, 1, published=True)
        execution_store = ExecutionStore(tmp_path / "execution.db")
        decision_bus = RiskDecisionBus()

        class FailingBroker:
            def dispatch(self, command, orders, now_ms):
                del command, orders, now_ms
                raise RuntimeError("local broker failed")

            def activate_protection(self, command, orders, now_ms):
                del command, orders, now_ms
                return ()

        failed_runtime = ExecutionRuntime(
            decision_bus,
            risk_store,
            engine(
                risk_store,
                execution_store,
                MarketProvider(),
                policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
                broker=FailingBroker(),
            ),
            ExecutionRuntimeSettings(
                "execution-failing",
                "execution-upstream-v1",
                outbox_flush_interval_ms=5,
            ),
            clock=lambda: NOW + 20,
        )
        with pytest.raises(ExecutionDispatchError, match="local simulation dispatch failed"):
            await failed_runtime.bootstrap()
        pending = execution_store.load_by_source_decision(
            "execution-primary",
            decision.decision_id,
        )
        assert pending is not None
        assert pending.delivery_state is ExecutionDeliveryState.PENDING
        assert (
            failed_runtime.replay_ledger.offset(
                "execution-upstream-v1",
                failed_runtime._durable_filter(),
            ).sequence
            == decision.storage_sequence
        )
        await failed_runtime.close()

        recovered_provider = MarketProvider()
        recovered_runtime = ExecutionRuntime(
            decision_bus,
            risk_store,
            engine(
                risk_store,
                execution_store,
                recovered_provider,
                policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
                broker=SimulatedBroker(),
            ),
            ExecutionRuntimeSettings(
                "execution-recovered",
                "execution-upstream-v1",
                outbox_flush_interval_ms=5,
            ),
            replay_ledger=RiskReplayLedger(risk_store),
            clock=lambda: NOW + 21,
        )
        await recovered_runtime.bootstrap()
        recovered = execution_store.load_by_source_decision(
            "execution-primary",
            decision.decision_id,
        )
        assert recovered is not None
        assert recovered.delivery_state is ExecutionDeliveryState.DISPATCHED
        assert recovered_provider.calls == 0
        assert recovered_runtime.health.replayed_decisions == 0
        await recovered_runtime.close()
        await decision_bus.close()

    asyncio.run(scenario())


def test_runtime_filters_are_pinned_to_approved_account_and_symbol() -> None:
    settings = ExecutionRuntimeSettings(
        "execution-live",
        "execution-upstream-v1",
        risk_engine_instance_ids=frozenset({"risk-primary"}),
        account_ids=frozenset({"paper"}),
        symbols=frozenset({SYMBOL}),
    )
    decision_filter = settings.decision_filter()
    assert decision_filter.engine_instance_ids == frozenset({"risk-primary"})
    assert decision_filter.account_ids == frozenset({"paper"})
    assert decision_filter.symbols == frozenset({SYMBOL})
    assert decision_filter.statuses == frozenset({RiskDecisionStatus.APPROVED})
