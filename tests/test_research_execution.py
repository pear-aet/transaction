from decimal import Decimal

import pytest

from crypto_quant.config import StrategyConfig
from crypto_quant.execution_contracts import (
    ExecutionCommandStatus,
    ExecutionPolicy,
    OrderLegType,
    OrderState,
)
from crypto_quant.portfolio_contracts import AccountPolicy
from crypto_quant.research_accounting import ResearchAccount
from crypto_quant.research_execution import (
    ResearchExecutionFactory,
    ResearchPlanningConflict,
)
from crypto_quant.risk_contracts import RiskApprovedAction, RiskDecisionStatus, RiskReason
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

from portfolio_fixtures import NOW, SYMBOL, market_rules


def intent(
    *,
    intent_id: str,
    action: StrategyAction,
    decision_time: int,
    reference_price: float,
    stop_price: float | None,
) -> StrategyIntent:
    return StrategyIntent(
        intent_id=intent_id,
        strategy_id="research-execution-test",
        strategy_version="1.0.0",
        instance_id="research-strategy",
        symbol=SYMBOL,
        action=action,
        decision_time=decision_time,
        created_at=decision_time,
        reference_price=reference_price,
        stop_price=stop_price,
        reason="research_execution_test",
        source_event_id=f"research-source-{intent_id}",
        source_sequence=0,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="research-parameter-hash",
        evidence={"test": True},
    )


def empty_account() -> ResearchAccount:
    return ResearchAccount.initialized(
        account_id="paper",
        initial_capital="10000",
        event_time=NOW - 1,
        source_id="research-execution-capital",
        policy=AccountPolicy(),
    )


def factory() -> ResearchExecutionFactory:
    return ResearchExecutionFactory(
        StrategyConfig(initial_cash=10_000),
        execution_policy=ExecutionPolicy(
            mode="simulation",
            max_market_deviation=0.01,
        ),
    )


@pytest.mark.parametrize(
    ("action", "stop", "approved_action", "primary_side"),
    (
        (StrategyAction.ENTER_LONG, 99.0, RiskApprovedAction.OPEN_LONG, "buy"),
        (StrategyAction.ENTER_SHORT, 101.0, RiskApprovedAction.OPEN_SHORT, "sell"),
    ),
)
def test_entry_planning_uses_real_risk_and_execution_contracts(
    action,
    stop,
    approved_action,
    primary_side,
) -> None:
    account = empty_account()
    planner = factory()
    selected_intent = intent(
        intent_id=f"research-entry-{action.value}",
        action=action,
        decision_time=NOW,
        reference_price=100,
        stop_price=stop,
    )

    first = planner.plan(
        account=account,
        intent=selected_intent,
        rules=market_rules(),
        risk_mark_price="100",
        execution_price="100.05",
        planned_at=NOW,
    )
    second = planner.plan(
        account=account,
        intent=selected_intent,
        rules=market_rules(),
        risk_mark_price="100",
        execution_price="100.05",
        planned_at=NOW,
    )

    assert second is first
    assert first.intent.storage_sequence == 1
    assert first.risk_decision.storage_sequence == 1
    assert first.risk_decision.status is RiskDecisionStatus.APPROVED
    assert first.risk_decision.approved_action is approved_action
    assert first.risk_decision.reasons == (RiskReason.APPROVED_ENTRY,)
    assert first.command is not None
    assert first.command.status is ExecutionCommandStatus.DISPATCH_PENDING
    assert first.command.source_decision_id == first.risk_decision.decision_id
    assert first.command.source_decision_storage_sequence == 1
    assert len(first.command.orders) == 2
    primary = next(
        order for order in first.command.orders if order.leg_type is OrderLegType.PRIMARY
    )
    protective = next(
        order
        for order in first.command.orders
        if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    assert primary.side.value == primary_side
    assert primary.reduce_only is False
    assert protective.reduce_only is True
    assert protective.trigger_price == stop
    assert first.command.evidence["submitted"] is False
    assert first.command.evidence["network_write"] is False

    with pytest.raises(ResearchPlanningConflict, match="plan changed"):
        planner.plan(
            account=account,
            intent=selected_intent,
            rules=market_rules(),
            risk_mark_price="100",
            execution_price="100.20",
            planned_at=NOW,
        )


def test_exit_planning_uses_current_research_position_snapshot() -> None:
    account = empty_account()
    planner = factory()
    entry_plan = planner.plan(
        account=account,
        intent=intent(
            intent_id="research-entry-for-exit",
            action=StrategyAction.ENTER_LONG,
            decision_time=NOW,
            reference_price=100,
            stop_price=99,
        ),
        rules=market_rules(),
        risk_mark_price=100,
        execution_price=100,
        planned_at=NOW,
    )
    assert entry_plan.command is not None
    account.reserve(
        command=entry_plan.command,
        decision=entry_plan.risk_decision,
        now_ms=NOW,
    )
    primary = next(
        order
        for order in entry_plan.command.orders
        if order.leg_type is OrderLegType.PRIMARY
    )
    quantity = Decimal(str(primary.quantity))
    quote = quantity * Decimal("100")
    fee = account.policy.quantize_money(quote * Decimal("0.001"))
    account.apply_open_update(
        command=entry_plan.command,
        decision=entry_plan.risk_decision,
        order=primary,
        update=planner.cumulative_fill_update(
            order=primary,
            event_time=NOW + 1,
            cumulative_quantity=quantity,
            cumulative_quote_value=quote,
            cumulative_fee=fee,
        ),
        maintenance_margin_rate=Decimal("0.005"),
    )

    exit_plan = planner.plan(
        account=account,
        intent=intent(
            intent_id="research-explicit-exit-plan",
            action=StrategyAction.EXIT,
            decision_time=NOW + 2,
            reference_price=101,
            stop_price=None,
        ),
        rules=market_rules(),
        risk_mark_price=101,
        execution_price=100.95,
        planned_at=NOW + 2,
    )

    assert exit_plan.risk_decision.status is RiskDecisionStatus.APPROVED
    assert exit_plan.risk_decision.approved_action is RiskApprovedAction.CLOSE_LONG
    assert exit_plan.risk_decision.reasons == (RiskReason.APPROVED_EXIT,)
    assert exit_plan.risk_decision.quantity == float(quantity)
    assert exit_plan.risk_decision.required_margin == 0
    assert exit_plan.risk_decision.reduce_only is True
    assert exit_plan.command is not None
    assert len(exit_plan.command.orders) == 1
    order = exit_plan.command.orders[0]
    assert order.leg_type is OrderLegType.PRIMARY
    assert order.reduce_only is True
    assert order.quantity == float(quantity)


def test_risk_rejection_and_execution_deviation_remain_explicit_facts() -> None:
    account = empty_account()
    rejected_planner = factory()
    rejected = rejected_planner.plan(
        account=account,
        intent=intent(
            intent_id="research-halted-entry",
            action=StrategyAction.ENTER_LONG,
            decision_time=NOW,
            reference_price=100,
            stop_price=99,
        ),
        rules=market_rules(),
        risk_mark_price=100,
        execution_price=100,
        planned_at=NOW,
        global_halt=True,
    )
    assert rejected.risk_decision.status is RiskDecisionStatus.REJECTED
    assert RiskReason.ACCOUNT_GLOBAL_HALT in rejected.risk_decision.reasons
    assert rejected.command is None
    assert rejected.execution_market is None

    deviation_planner = factory()
    deviated = deviation_planner.plan(
        account=account,
        intent=intent(
            intent_id="research-price-deviation",
            action=StrategyAction.ENTER_LONG,
            decision_time=NOW,
            reference_price=100,
            stop_price=99,
        ),
        rules=market_rules(),
        risk_mark_price=100,
        execution_price=102,
        planned_at=NOW,
    )
    assert deviated.risk_decision.status is RiskDecisionStatus.APPROVED
    assert deviated.command is not None
    assert deviated.command.status is ExecutionCommandStatus.EXPIRED
    assert deviated.command.quantity == 0
    assert deviated.command.orders == ()


def test_cumulative_fill_updates_are_deterministic_and_exchange_safe() -> None:
    account = empty_account()
    planner = factory()
    plan = planner.plan(
        account=account,
        intent=intent(
            intent_id="research-fill-update",
            action=StrategyAction.ENTER_LONG,
            decision_time=NOW,
            reference_price=100,
            stop_price=99,
        ),
        rules=market_rules(),
        risk_mark_price=100,
        execution_price=100,
        planned_at=NOW,
    )
    assert plan.command is not None
    order = next(
        order for order in plan.command.orders if order.leg_type is OrderLegType.PRIMARY
    )
    quantity = Decimal(str(order.quantity))
    partial = quantity / Decimal("2")
    first = planner.cumulative_fill_update(
        order=order,
        event_time=NOW + 1,
        cumulative_quantity=partial,
        cumulative_quote_value=partial * Decimal("100"),
        cumulative_fee=Decimal("0.01"),
        evidence={"slice": 1},
    )
    replay = planner.cumulative_fill_update(
        order=order,
        event_time=NOW + 1,
        cumulative_quantity=partial,
        cumulative_quote_value=partial * Decimal("100"),
        cumulative_fee=Decimal("0.01"),
        evidence={"slice": 1},
    )
    full = planner.cumulative_fill_update(
        order=order,
        event_time=NOW + 2,
        cumulative_quantity=quantity,
        cumulative_quote_value=quantity * Decimal("101"),
        cumulative_fee=Decimal("0.02"),
        evidence={"slice": 2},
    )

    assert replay.to_dict() == first.to_dict()
    assert first.new_state is OrderState.PARTIALLY_FILLED
    assert full.new_state is OrderState.FILLED
    assert first.update_id != full.update_id
    assert first.broker_order_id == replay.broker_order_id
    assert first.evidence["submitted"] is False
    assert first.evidence["network_write"] is False
