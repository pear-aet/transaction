import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution import apply_order_update, build_execution_command
from crypto_quant.execution_broker import (
    ExecutionBrokerError,
    ShadowBroker,
    SimulatedBroker,
)
from crypto_quant.execution_contracts import (
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    OrderState,
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


def simulation_command():
    rules = MarketRules(
        SYMBOL,
        tick_size=0.1,
        step_size=0.001,
        min_quantity=0.001,
        min_notional=5,
        max_leverage=100,
        brackets=(RiskBracket(0, 1_000_000, 0.005),),
    )
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
        reason="execution_broker_test",
        source_event_id="market-entry",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=1,
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
        rules,
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
    context = ExecutionContext(
        "execution-primary",
        decision.with_storage_sequence(1),
        ExecutionMarketSnapshot(
            "execution-market",
            SYMBOL,
            NOW,
            100,
            99.9,
            100.1,
        ),
        ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        NOW,
    )
    return build_execution_command(context)


def apply_all(order, updates):
    current = order
    for update in updates:
        current = apply_order_update(current, update)
    return current


def test_shadow_broker_has_no_dispatch_path() -> None:
    command = simulation_command().with_storage_sequence(1)
    with pytest.raises(ExecutionBrokerError, match="shadow execution has no dispatch"):
        ShadowBroker().dispatch(command, command.orders, NOW)


def test_simulated_dispatch_is_deterministic_local_and_unfilled() -> None:
    command = simulation_command().with_storage_sequence(1)
    broker = SimulatedBroker()

    first = broker.dispatch(command, command.orders, NOW + 1)
    second = broker.dispatch(command, command.orders, NOW + 1)

    assert first == second
    assert [update.new_state for update in first] == [
        OrderState.DISPATCHED,
        OrderState.ACKNOWLEDGED,
    ]
    assert all(update.cumulative_filled_quantity == 0 for update in first)
    assert all(update.evidence["submitted"] is False for update in first)
    assert all(update.evidence["network_write"] is False for update in first)
    primary = apply_all(command.orders[0], first)
    assert primary.state is OrderState.ACKNOWLEDGED
    assert primary.broker_order_id is not None


def test_partial_fill_activates_reduce_only_protection() -> None:
    command = simulation_command().with_storage_sequence(1)
    broker = SimulatedBroker()
    primary, protective = command.orders
    primary = apply_all(primary, broker.dispatch(command, command.orders, NOW + 1))

    half = primary.quantity / 2
    partial_update = broker.fill(
        primary,
        fill_quantity=half,
        fill_price=100.1,
        fee=0.1,
        event_time=NOW + 2,
    )
    primary = apply_order_update(primary, partial_update)
    assert primary.state is OrderState.PARTIALLY_FILLED

    activation = broker.activate_protection(
        command,
        (primary, protective),
        NOW + 3,
    )
    protective = apply_all(protective, activation)
    assert protective.state is OrderState.ACKNOWLEDGED
    assert protective.reduce_only is True
    assert protective.quantity == command.quantity
    assert protective.activation_order_id == primary.order_id

    final_update = broker.fill(
        primary,
        fill_quantity=primary.quantity - primary.cumulative_filled_quantity,
        fill_price=100.2,
        fee=0.1,
        event_time=NOW + 4,
    )
    primary = apply_order_update(primary, final_update)
    assert primary.state is OrderState.FILLED
    assert primary.cumulative_filled_quantity == primary.quantity


def test_simulated_cancel_and_rejection_preserve_terminal_monotonicity() -> None:
    command = simulation_command().with_storage_sequence(1)
    broker = SimulatedBroker()
    primary = apply_all(
        command.orders[0],
        broker.dispatch(command, command.orders, NOW + 1),
    )

    canceled = apply_all(
        primary,
        broker.cancel(primary, event_time=NOW + 2, reason="test_cancel"),
    )
    assert canceled.state is OrderState.CANCELED
    with pytest.raises(ExecutionBrokerError, match="cannot fill"):
        broker.fill(
            canceled,
            fill_quantity=1,
            fill_price=100,
            fee=0,
            event_time=NOW + 3,
        )

    command = simulation_command().with_storage_sequence(1)
    dispatched = apply_order_update(
        command.orders[0],
        broker.dispatch(command, command.orders, NOW + 1)[0],
    )
    rejected = apply_order_update(
        dispatched,
        broker.reject(dispatched, event_time=NOW + 2, reason="test_reject"),
    )
    assert rejected.state is OrderState.REJECTED


def test_simulated_broker_rejects_unpersisted_or_expired_commands() -> None:
    command = simulation_command()
    broker = SimulatedBroker()
    with pytest.raises(ExecutionBrokerError, match="persisted before dispatch"):
        broker.dispatch(command, command.orders, NOW + 1)

    stored = command.with_storage_sequence(1)
    with pytest.raises(ExecutionBrokerError, match="expired simulation command"):
        broker.dispatch(stored, stored.orders, stored.valid_until + 1)
