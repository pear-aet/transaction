from dataclasses import replace

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution import build_execution_command
from crypto_quant.execution_broker import SimulatedBroker
from crypto_quant.execution_contracts import (
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    OrderState,
    OrderUpdate,
)
from crypto_quant.execution_store import (
    ExecutionCommandConflict,
    ExecutionDeliveryState,
    ExecutionInstanceError,
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


def risk_decision(storage_sequence: int = 1):
    intent = StrategyIntent(
        intent_id="intent-entry",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=DECISION_TIME,
        created_at=DECISION_TIME,
        reference_price=100,
        stop_price=99,
        reason="execution_store_test",
        source_event_id="market-entry",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=storage_sequence,
    )
    account = AccountRiskSnapshot(
        "account-snapshot",
        "paper",
        DECISION_TIME,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    market = MarketRiskSnapshot(
        "risk-market-snapshot",
        SYMBOL,
        DECISION_TIME,
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
            NOW,
        )
    )
    assert decision.status is RiskDecisionStatus.APPROVED
    return decision.with_storage_sequence(storage_sequence)


def command(
    *,
    policy: ExecutionPolicy | None = None,
    engine_instance_id: str = "execution-primary",
):
    selected_policy = policy or ExecutionPolicy()
    context = ExecutionContext(
        engine_instance_id,
        risk_decision(),
        ExecutionMarketSnapshot(
            "execution-market",
            SYMBOL,
            NOW,
            100,
            99.9,
            100.1,
        ),
        selected_policy,
        NOW,
    )
    return build_execution_command(context)


def test_shadow_command_and_orders_commit_atomically_and_idempotently(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "quant.db")
    value = command()
    commit = store.commit_command(value, updated_at=NOW)

    assert commit.duplicate is False
    assert commit.command_inserted is True
    assert commit.stored_command.delivery_state is ExecutionDeliveryState.RECORDED
    assert commit.stored_command.command.storage_sequence == 1
    assert [order.order_id for order in commit.stored_command.orders] == [
        value.orders[0].order_id,
        value.orders[1].order_id,
    ]
    assert store.metrics() == {
        "instances": 1,
        "commands": 1,
        "recorded_commands": 1,
        "pending_commands": 0,
        "dispatched_commands": 0,
        "completed_commands": 0,
        "expired_commands": 0,
        "failed_commands": 0,
        "orders": 2,
        "planned_orders": 2,
        "partially_filled_orders": 0,
        "filled_orders": 0,
        "failed_orders": 0,
        "order_updates": 0,
        "command_transitions": 0,
        "latest_command_sequence": 1,
    }

    duplicate = store.commit_command(value, updated_at=NOW + 1)
    assert duplicate.duplicate is True
    assert duplicate.command_inserted is False
    assert duplicate.stored_command.command.storage_sequence == 1
    assert store.metrics()["commands"] == 1


def test_one_risk_decision_cannot_change_execution_semantics(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "quant.db")
    value = command()
    store.commit_command(value, updated_at=NOW)

    changed = replace(value, approval_price=value.approval_price + 0.1)
    with pytest.raises(ExecutionCommandConflict, match="semantics changed"):
        store.commit_command(changed, updated_at=NOW + 1)


def test_execution_instance_pins_policy_and_mode(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "quant.db")
    store.commit_command(command(), updated_at=NOW)
    changed_policy = ExecutionPolicy(
        policy_id="simulation-policy",
        mode=ExecutionMode.SIMULATION,
    )

    with pytest.raises(ExecutionInstanceError, match="use a new engine_instance_id"):
        store.commit_command(command(policy=changed_policy), updated_at=NOW + 1)


def test_simulation_outbox_and_delivery_transitions_are_monotonic(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "quant.db")
    value = command(policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION))
    stored = store.commit_command(value, updated_at=NOW).stored_command
    broker = SimulatedBroker()

    assert stored.delivery_state is ExecutionDeliveryState.PENDING
    assert [item.command.command_id for item in store.pending_commands()] == [value.command_id]
    assert store.oldest_pending_created_at() == NOW
    for update in broker.dispatch(stored.command, stored.orders, NOW + 1):
        store.apply_order_update(update)
    assert store.mark_dispatched(value.command_id, NOW + 1) is True
    assert store.mark_dispatched(value.command_id, NOW + 2) is False

    current = store.load_command(value.command_id)
    assert current is not None
    primary = current.orders[0]
    store.apply_order_update(
        broker.fill(
            primary,
            fill_quantity=primary.quantity,
            fill_price=100.1,
            fee=0.1,
            event_time=NOW + 2,
        )
    )
    current = store.load_command(value.command_id)
    assert current is not None
    for update in broker.activate_protection(current.command, current.orders, NOW + 2):
        store.apply_order_update(update)
    current = store.load_command(value.command_id)
    assert current is not None
    protective = current.orders[1]
    store.apply_order_update(
        broker.fill(
            protective,
            fill_quantity=protective.quantity,
            fill_price=99,
            fee=0.1,
            event_time=NOW + 3,
        )
    )
    assert store.mark_completed(value.command_id, NOW + 3) is True
    assert store.mark_completed(value.command_id, NOW + 4) is False
    with pytest.raises(ValueError, match="cannot move"):
        store.mark_failed(value.command_id, NOW + 5, "late_failure")

    transitions = store.command_transitions(value.command_id)
    assert [item.new_state for item in transitions] == [
        ExecutionDeliveryState.DISPATCHED,
        ExecutionDeliveryState.COMPLETED,
    ]
    assert all(item.source_decision_id == value.source_decision_id for item in transitions)


def test_expired_pending_command_cannot_be_dispatched(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "quant.db")
    value = command(policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION))
    store.commit_command(value, updated_at=NOW)

    with pytest.raises(ValueError, match="expired execution command"):
        store.mark_dispatched(value.command_id, value.valid_until + 1)
    assert store.mark_expired(value.command_id, value.valid_until + 1) is True
    assert store.mark_expired(value.command_id, value.valid_until + 2) is False
    assert store.pending_commands() == []


def test_order_updates_are_append_only_monotonic_and_idempotent(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "quant.db")
    value = command(policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION))
    stored = store.commit_command(value, updated_at=NOW).stored_command
    primary = stored.orders[0]
    broker_id = "sim-order-primary"

    dispatched = OrderUpdate(
        "update-dispatched",
        primary.order_id,
        NOW + 1,
        OrderState.DISPATCHED,
        0,
        0,
        0,
        broker_order_id=broker_id,
        evidence={"source": "simulated_broker"},
    )
    first = store.apply_order_update(dispatched)
    assert first.order.state is OrderState.DISPATCHED
    assert first.prior_state is OrderState.PLANNED
    assert first.update_storage_sequence == 1

    acknowledged = OrderUpdate(
        "update-acknowledged",
        primary.order_id,
        NOW + 2,
        OrderState.ACKNOWLEDGED,
        0,
        0,
        0,
        broker_order_id=broker_id,
        evidence={"source": "simulated_broker"},
    )
    store.apply_order_update(acknowledged)
    store.mark_dispatched(value.command_id, NOW + 2)
    quantity = primary.quantity / 2
    price = 100.1
    partial = OrderUpdate(
        "update-partial",
        primary.order_id,
        NOW + 3,
        OrderState.PARTIALLY_FILLED,
        quantity,
        quantity * price,
        0.1,
        average_fill_price=price,
        broker_order_id=broker_id,
        evidence={"source": "simulated_fill"},
    )
    result = store.apply_order_update(partial)
    assert result.order.state is OrderState.PARTIALLY_FILLED
    assert result.order.cumulative_filled_quantity == quantity

    duplicate = store.apply_order_update(partial)
    assert duplicate.duplicate is True
    assert duplicate.update_storage_sequence == 3
    with pytest.raises(ExecutionUpdateConflict, match="identifier collision"):
        store.apply_order_update(replace(partial, cumulative_fee=0.2))

    journal = store.order_updates(primary.order_id)
    assert [item.update.update_id for item in journal] == [
        "update-dispatched",
        "update-acknowledged",
        "update-partial",
    ]
    assert [item.prior_state for item in journal] == [
        OrderState.PLANNED,
        OrderState.DISPATCHED,
        OrderState.ACKNOWLEDGED,
    ]
    assert store.metrics()["order_updates"] == 3
