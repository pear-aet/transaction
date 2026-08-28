from dataclasses import replace

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution import (
    ExecutionCommandMismatch,
    OrderTransitionError,
    apply_order_update,
    build_execution_command,
    validate_execution_command,
)
from crypto_quant.execution_contracts import (
    ExecutionCommand,
    ExecutionCommandStatus,
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    ExecutionReason,
    ExecutionSide,
    OrderLegType,
    OrderState,
    OrderUpdate,
)
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    PositionRiskSnapshot,
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


def strategy_intent(action: StrategyAction = StrategyAction.ENTER_LONG) -> StrategyIntent:
    return StrategyIntent(
        intent_id=f"intent-{action.value}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=action,
        decision_time=DECISION_TIME,
        created_at=DECISION_TIME,
        reference_price=100,
        stop_price=99 if action is StrategyAction.ENTER_LONG else None,
        reason="execution_test",
        source_event_id=f"market-{action.value}",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=1,
    )


def risk_decision(action: StrategyAction = StrategyAction.ENTER_LONG):
    position = None
    if action is StrategyAction.EXIT:
        position = PositionRiskSnapshot(
            SYMBOL,
            "long",
            quantity=2,
            entry_price=100,
            mark_price=100,
            initial_risk=50,
            maintenance_margin_rate=0.005,
            stop_price=90,
        )
    account = AccountRiskSnapshot(
        "account-snapshot",
        "paper",
        DECISION_TIME,
        10_000,
        10_000 if position is None else 9_000,
        0 if position is None else position.notional * position.maintenance_margin_rate,
        10_000,
        10_000,
        0,
        () if position is None else (position,),
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
            strategy_intent(action),
            account,
            market,
            RiskPolicy(),
            NOW,
        )
    )
    assert decision.status is RiskDecisionStatus.APPROVED
    return decision.with_storage_sequence(1)


def execution_market(
    *,
    observed_at: int = NOW,
    mark_price: float = 100,
    bid_price: float | None = 99.9,
    ask_price: float | None = 100.1,
) -> ExecutionMarketSnapshot:
    return ExecutionMarketSnapshot(
        "execution-market-snapshot",
        SYMBOL,
        observed_at,
        mark_price,
        bid_price,
        ask_price,
    )


def context(
    *,
    action: StrategyAction = StrategyAction.ENTER_LONG,
    policy: ExecutionPolicy | None = None,
    market: ExecutionMarketSnapshot | None = None,
    planned_at: int = NOW,
) -> ExecutionContext:
    return ExecutionContext(
        "execution-primary",
        risk_decision(action),
        market or execution_market(),
        policy or ExecutionPolicy(),
        planned_at,
    )


def test_shadow_entry_plan_is_deterministic_and_never_submitted() -> None:
    first = build_execution_command(context())
    second = build_execution_command(context())

    assert first == second
    assert first.status is ExecutionCommandStatus.SHADOW_RECORDED
    assert first.reasons == (ExecutionReason.SHADOW_ONLY,)
    assert first.mode is ExecutionMode.SHADOW
    assert len(first.orders) == 2
    primary, stop = first.orders
    assert primary.leg_type is OrderLegType.PRIMARY
    assert primary.side is ExecutionSide.BUY
    assert primary.reduce_only is False
    assert stop.leg_type is OrderLegType.PROTECTIVE_STOP
    assert stop.side is ExecutionSide.SELL
    assert stop.reduce_only is True
    assert stop.activation_order_id == primary.order_id
    assert stop.trigger_price == first.stop_price
    assert all(order.evidence["submitted"] is False for order in first.orders)
    assert all(order.evidence["network_write"] is False for order in first.orders)
    assert all(len(order.client_order_id) <= 36 for order in first.orders)
    assert ExecutionCommand.from_dict(first.to_dict()) == first


def test_simulation_entry_is_dispatch_pending_with_same_risk_plan() -> None:
    shadow = build_execution_command(context())
    simulation = build_execution_command(
        context(policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION))
    )

    assert simulation.status is ExecutionCommandStatus.DISPATCH_PENDING
    assert simulation.reasons == (ExecutionReason.READY,)
    assert simulation.quantity == shadow.quantity
    assert simulation.stop_price == shadow.stop_price
    assert simulation.leverage == shadow.leverage
    assert simulation.required_margin == shadow.required_margin
    assert simulation.command_id != shadow.command_id


def test_exit_plan_has_one_reduce_only_primary_order() -> None:
    command = build_execution_command(context(action=StrategyAction.EXIT))

    assert command.status is ExecutionCommandStatus.SHADOW_RECORDED
    assert command.reduce_only is True
    assert command.stop_price is None
    assert len(command.orders) == 1
    order = command.orders[0]
    assert order.leg_type is OrderLegType.PRIMARY
    assert order.side is ExecutionSide.SELL
    assert order.reduce_only is True


def test_expired_risk_decision_produces_no_order_plan() -> None:
    current = context()
    expired = replace(current.decision, valid_until=NOW)
    command = build_execution_command(replace(current, decision=expired, planned_at=NOW + 1))

    assert command.status is ExecutionCommandStatus.EXPIRED
    assert command.reasons == (ExecutionReason.RISK_DECISION_EXPIRED,)
    assert command.orders == ()
    assert command.quantity == 0
    assert command.evidence["submitted"] is False


def test_stale_future_and_moved_market_fail_closed() -> None:
    policy = ExecutionPolicy(max_market_age_ms=10, max_market_deviation=0.01)
    stale = build_execution_command(
        context(
            policy=policy,
            market=execution_market(observed_at=NOW - 11),
            planned_at=NOW,
        )
    )
    future = build_execution_command(
        context(
            policy=policy,
            market=execution_market(observed_at=NOW + 1),
            planned_at=NOW,
        )
    )
    moved = build_execution_command(
        context(
            policy=policy,
            market=execution_market(mark_price=105, bid_price=104.9, ask_price=105.1),
        )
    )

    assert stale.reasons == (ExecutionReason.MARKET_SNAPSHOT_STALE,)
    assert future.reasons == (ExecutionReason.MARKET_SNAPSHOT_FUTURE,)
    assert moved.reasons == (ExecutionReason.MARKET_PRICE_DEVIATION,)
    assert all(command.orders == () for command in (stale, future, moved))


def test_command_validation_rejects_changed_semantics() -> None:
    selected_context = context()
    command = build_execution_command(selected_context)
    validate_execution_command(selected_context, command)

    with pytest.raises(ExecutionCommandMismatch, match="approval_price"):
        validate_execution_command(
            selected_context,
            replace(command, approval_price=command.approval_price + 0.1),
        )


def test_storage_sequence_does_not_change_command_semantics() -> None:
    command = build_execution_command(context())
    stored = command.with_storage_sequence(9)

    assert stored.storage_sequence == 9
    assert stored.orders == command.orders
    assert stored.semantic_dict() == command.semantic_dict()


def test_order_state_machine_accepts_partial_then_complete_fill() -> None:
    order = build_execution_command(
        context(policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION))
    ).orders[0]
    dispatched = apply_order_update(
        order,
        OrderUpdate("u1", order.order_id, NOW + 1, OrderState.DISPATCHED, 0, 0, 0),
    )
    acknowledged = apply_order_update(
        dispatched,
        OrderUpdate(
            "u2",
            order.order_id,
            NOW + 2,
            OrderState.ACKNOWLEDGED,
            0,
            0,
            0,
            broker_order_id="sim-1",
        ),
    )
    partial_quantity = order.quantity / 2
    partial = apply_order_update(
        acknowledged,
        OrderUpdate(
            "u3",
            order.order_id,
            NOW + 3,
            OrderState.PARTIALLY_FILLED,
            partial_quantity,
            partial_quantity * 100.2,
            0.01,
            average_fill_price=100.2,
            broker_order_id="sim-1",
        ),
    )
    filled = apply_order_update(
        partial,
        OrderUpdate(
            "u4",
            order.order_id,
            NOW + 4,
            OrderState.FILLED,
            order.quantity,
            order.quantity * 100.25,
            0.02,
            average_fill_price=100.25,
            broker_order_id="sim-1",
        ),
    )

    assert partial.remaining_quantity == pytest.approx(order.quantity / 2)
    assert filled.terminal is True
    assert filled.remaining_quantity == 0


def test_order_state_machine_rejects_backwards_and_terminal_updates() -> None:
    order = build_execution_command(
        context(policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION))
    ).orders[0]
    dispatched = apply_order_update(
        order,
        OrderUpdate("u1", order.order_id, NOW + 1, OrderState.DISPATCHED, 0, 0, 0),
    )
    acknowledged = apply_order_update(
        dispatched,
        OrderUpdate("u2", order.order_id, NOW + 2, OrderState.ACKNOWLEDGED, 0, 0, 0),
    )
    quantity = order.quantity / 2
    partial = apply_order_update(
        acknowledged,
        OrderUpdate(
            "u3",
            order.order_id,
            NOW + 3,
            OrderState.PARTIALLY_FILLED,
            quantity,
            quantity * 100,
            0,
            average_fill_price=100,
        ),
    )

    with pytest.raises(OrderTransitionError, match="increase"):
        apply_order_update(
            partial,
            OrderUpdate(
                "u4",
                order.order_id,
                NOW + 4,
                OrderState.PARTIALLY_FILLED,
                quantity,
                quantity * 100,
                0,
                average_fill_price=100,
            ),
        )
    with pytest.raises(OrderTransitionError, match="backwards"):
        apply_order_update(
            partial,
            OrderUpdate(
                "u5",
                order.order_id,
                NOW + 2,
                OrderState.FILLED,
                order.quantity,
                order.quantity * 100,
                0,
                average_fill_price=100,
            ),
        )

    filled = apply_order_update(
        partial,
        OrderUpdate(
            "u6",
            order.order_id,
            NOW + 5,
            OrderState.FILLED,
            order.quantity,
            order.quantity * 100,
            0,
            average_fill_price=100,
        ),
    )
    with pytest.raises(OrderTransitionError, match="terminal"):
        apply_order_update(
            filled,
            OrderUpdate(
                "u7",
                order.order_id,
                NOW + 6,
                OrderState.CANCELED,
                order.quantity,
                order.quantity * 100,
                0,
                average_fill_price=100,
            ),
        )
