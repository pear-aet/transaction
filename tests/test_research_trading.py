from decimal import Decimal

from crypto_quant.config import StrategyConfig
from crypto_quant.execution_contracts import ExecutionMode, ExecutionPolicy
from crypto_quant.portfolio_contracts import AccountPolicy, ReservationState
from crypto_quant.research_accounting import ResearchAccount
from crypto_quant.research_execution import ResearchExecutionFactory
from crypto_quant.research_trading import ResearchTradeCoordinator
from crypto_quant.risk_contracts import RiskApprovedAction, RiskDecisionStatus
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

from portfolio_fixtures import NOW, SYMBOL, market_rules


def intent(
    *,
    identifier: str,
    action: StrategyAction,
    timestamp: int,
    reference_price: float,
    stop_price: float | None,
) -> StrategyIntent:
    return StrategyIntent(
        intent_id=identifier,
        strategy_id="research-trading-test",
        strategy_version="1.0.0",
        instance_id="research-trading",
        symbol=SYMBOL,
        action=action,
        decision_time=timestamp,
        created_at=timestamp,
        reference_price=reference_price,
        stop_price=stop_price,
        reason=identifier,
        source_event_id=f"source:{identifier}",
        source_sequence=0,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="research-trading-parameters",
        evidence={"test": identifier},
    )


def coordinator() -> ResearchTradeCoordinator:
    account = ResearchAccount.initialized(
        account_id="paper",
        initial_capital="10000",
        event_time=NOW - 1,
        source_id="research-trading-capital",
        policy=AccountPolicy(),
    )
    planner = ResearchExecutionFactory(
        StrategyConfig(initial_cash=10_000),
        execution_policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
    )
    return ResearchTradeCoordinator(
        account=account,
        planner=planner,
        rules={SYMBOL: market_rules()},
        fee_rate="0.001",
        maintenance_margin_rate="0.005",
    )


def test_rejected_entry_creates_no_position_reservation_or_fill() -> None:
    trading = coordinator()
    before = trading.account.balances

    result = trading.open_full(
        intent=intent(
            identifier="halted-entry",
            action=StrategyAction.ENTER_LONG,
            timestamp=NOW,
            reference_price=100,
            stop_price=99,
        ),
        risk_mark_price=100,
        execution_price=100,
        event_time=NOW,
        global_halt=True,
    )

    assert result.disposition == "risk_rejected"
    assert result.plan.risk_decision.status is RiskDecisionStatus.REJECTED
    assert result.plan.command is None
    assert result.order is None
    assert result.update is None
    assert result.position is None
    assert trading.account.positions == {}
    assert trading.account.reservations == {}
    assert trading.account.balances == before
    assert len(trading.account.journal) == 1


def test_protective_close_reuses_entry_command_and_releases_all_reserves() -> None:
    trading = coordinator()
    opened = trading.open_full(
        intent=intent(
            identifier="protective-entry",
            action=StrategyAction.ENTER_LONG,
            timestamp=NOW,
            reference_price=100,
            stop_price=99,
        ),
        risk_mark_price=100,
        execution_price=100,
        event_time=NOW,
    )
    assert opened.position is not None
    assert opened.plan.command is not None
    trading.apply_funding(
        symbol=SYMBOL,
        rate="0.001",
        mark_price="100",
        event_time=NOW + 1,
        source_id="protective-funding",
    )

    closed = trading.close_full(
        symbol=SYMBOL,
        execution_price="99",
        event_time=NOW + 2,
        reason="stop_loss",
    )

    assert closed.disposition == "filled"
    assert closed.plan.command is opened.plan.command
    assert closed.order is not None
    assert closed.order.reduce_only is True
    assert closed.position is None
    assert trading.account.positions == {}
    assert trading.entry_plans == {}
    assert len(trading.account.closed_positions) == 1
    fact = trading.account.closed_positions[0]
    assert fact.open_command_id == opened.plan.command.command_id
    assert fact.close_command_id == opened.plan.command.command_id
    reservation = trading.account.reservations[opened.plan.command.command_id]
    assert reservation.state is ReservationState.RELEASED
    assert trading.account.balances.command_margin == Decimal("0")
    assert trading.account.balances.position_margin == Decimal("0")
    assert trading.account.balances.command_fee_reserve == Decimal("0")


def test_explicit_exit_uses_new_risk_decision_and_reduce_only_command() -> None:
    trading = coordinator()
    opened = trading.open_full(
        intent=intent(
            identifier="explicit-entry",
            action=StrategyAction.ENTER_SHORT,
            timestamp=NOW,
            reference_price=100,
            stop_price=101,
        ),
        risk_mark_price=100,
        execution_price=100,
        event_time=NOW,
    )
    assert opened.position is not None
    assert opened.plan.command is not None

    closed = trading.close_full(
        symbol=SYMBOL,
        execution_price="98",
        risk_mark_price="98",
        event_time=NOW + 2,
        exit_intent=intent(
            identifier="explicit-exit",
            action=StrategyAction.EXIT,
            timestamp=NOW + 2,
            reference_price=98,
            stop_price=None,
        ),
        reason="strategy_exit",
    )

    assert closed.disposition == "filled"
    assert closed.plan.risk_decision.status is RiskDecisionStatus.APPROVED
    assert closed.plan.risk_decision.approved_action is RiskApprovedAction.CLOSE_SHORT
    assert closed.plan.command is not None
    assert closed.plan.command.command_id != opened.plan.command.command_id
    assert closed.order is not None
    assert closed.order.reduce_only is True
    assert closed.position is None
    fact = trading.account.closed_positions[0]
    assert fact.open_command_id == opened.plan.command.command_id
    assert fact.close_command_id == closed.plan.command.command_id
    assert fact.realized_pnl > 0
    assert trading.account.reservations[opened.plan.command.command_id].state is (
        ReservationState.RELEASED
    )
    assert trading.account.reservations[closed.plan.command.command_id].state is (
        ReservationState.RELEASED
    )
