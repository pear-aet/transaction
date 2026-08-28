from dataclasses import replace

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    PositionRiskSnapshot,
    RiskApprovedAction,
    RiskContext,
    RiskDecisionStatus,
    RiskPolicy,
    RiskReason,
)
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
DECISION_TIME = 900_000


def rules(
    *,
    min_notional: float = 5.0,
    brackets: tuple[RiskBracket, ...] | None = None,
) -> MarketRules:
    return MarketRules(
        SYMBOL,
        tick_size=0.1,
        step_size=0.001,
        min_quantity=0.001,
        min_notional=min_notional,
        max_leverage=100,
        brackets=brackets or (RiskBracket(0, 1_000_000, 0.005),),
    )


def intent(
    action: StrategyAction = StrategyAction.ENTER_LONG,
    *,
    reference_price: float = 100.0,
    stop_price: float | None = 99.0,
) -> StrategyIntent:
    if action is StrategyAction.EXIT:
        stop_price = None
    return StrategyIntent(
        intent_id=f"intent-{action.value}-{reference_price}-{stop_price}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=action,
        decision_time=DECISION_TIME,
        created_at=DECISION_TIME + 1,
        reference_price=reference_price,
        stop_price=stop_price,
        reason="test",
        source_event_id="market-1",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=1,
    )


def account(
    *,
    equity: float = 10_000,
    available_balance: float = 10_000,
    positions: tuple[PositionRiskSnapshot, ...] = (),
    observed_at: int = DECISION_TIME,
    global_halt: bool = False,
) -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        snapshot_id=f"account-{observed_at}-{len(positions)}-{global_halt}",
        account_id="paper",
        observed_at=observed_at,
        equity=equity,
        available_balance=available_balance,
        maintenance_margin=sum(
            position.notional * position.maintenance_margin_rate for position in positions
        ),
        day_start_equity=10_000,
        peak_equity=10_000,
        consecutive_losses=0,
        positions=positions,
        global_halt=global_halt,
    )


def market(
    *,
    mark_price: float = 100.0,
    observed_at: int = DECISION_TIME,
    volatility: float | None = None,
    market_rules: MarketRules | None = None,
) -> MarketRiskSnapshot:
    return MarketRiskSnapshot(
        snapshot_id=f"market-{mark_price}-{observed_at}-{volatility}",
        symbol=SYMBOL,
        observed_at=observed_at,
        mark_price=mark_price,
        rules=market_rules or rules(),
        correlation_group="majors",
        volatility_annualized=volatility,
    )


def context(
    *,
    strategy_intent: StrategyIntent | None = None,
    account_snapshot: AccountRiskSnapshot | None = None,
    market_snapshot: MarketRiskSnapshot | None = None,
    policy: RiskPolicy | None = None,
    evaluated_at: int = DECISION_TIME + 1,
) -> RiskContext:
    return RiskContext(
        "risk-primary",
        strategy_intent or intent(),
        account_snapshot or account(),
        market_snapshot or market(),
        policy or RiskPolicy(),
        evaluated_at,
    )


def test_entry_approval_is_deterministic_and_budget_bounded() -> None:
    first = evaluate_risk(context())
    second = evaluate_risk(context(evaluated_at=DECISION_TIME + 2))

    assert first.status is RiskDecisionStatus.APPROVED
    assert first.approved_action is RiskApprovedAction.OPEN_LONG
    assert first.quantity > 0
    assert first.initial_risk <= first.risk_budget + 1e-9
    assert first.portfolio_risk_after <= 300.0 + 1e-9
    assert first.symbol_risk_after <= 150.0 + 1e-9
    assert first.liquidation_buffer_ratio >= 0.02
    assert first.decision_id == second.decision_id
    assert first.semantic_dict() == second.semantic_dict()


def test_high_volatility_reduces_approved_risk_and_size() -> None:
    normal = evaluate_risk(context(market_snapshot=market(volatility=0.8)))
    high = evaluate_risk(context(market_snapshot=market(volatility=3.2)))

    assert normal.status is RiskDecisionStatus.APPROVED
    assert high.status is RiskDecisionStatus.APPROVED
    assert high.risk_budget < normal.risk_budget
    assert high.quantity < normal.quantity
    assert high.leverage <= normal.leverage


def test_stale_intent_and_reference_deviation_fail_closed() -> None:
    stale = evaluate_risk(
        context(
            policy=replace(RiskPolicy(), max_intent_age_ms=100),
            evaluated_at=DECISION_TIME + 101,
        )
    )
    moved = evaluate_risk(
        context(
            market_snapshot=market(mark_price=105),
            policy=replace(RiskPolicy(), max_reference_deviation=0.01),
        )
    )

    assert stale.status is RiskDecisionStatus.REJECTED
    assert RiskReason.STALE_INTENT in stale.reasons
    assert moved.status is RiskDecisionStatus.REJECTED
    assert RiskReason.REFERENCE_DEVIATION in moved.reasons


def test_existing_portfolio_risk_and_market_minimums_reject_entry() -> None:
    existing = PositionRiskSnapshot(
        SYMBOL.replace("BTC", "ETH"),
        "long",
        quantity=10,
        entry_price=100,
        mark_price=100,
        initial_risk=300,
        maintenance_margin_rate=0.005,
        correlation_group="majors",
    )
    exhausted = evaluate_risk(context(account_snapshot=account(positions=(existing,))))
    too_small = evaluate_risk(
        context(market_snapshot=market(market_rules=rules(min_notional=20_000)))
    )

    assert exhausted.status is RiskDecisionStatus.REJECTED
    assert RiskReason.PORTFOLIO_RISK_LIMIT in exhausted.reasons
    assert RiskReason.GROUP_RISK_LIMIT in exhausted.reasons
    assert too_small.status is RiskDecisionStatus.REJECTED
    assert RiskReason.MARKET_MINIMUM in too_small.reasons


def test_invalid_bracket_coverage_fails_closed() -> None:
    broken_rules = rules(
        brackets=(
            RiskBracket(0, 10_000, 0.005),
            RiskBracket(20_000, 1_000_000, 0.01),
        )
    )
    decision = evaluate_risk(context(market_snapshot=market(market_rules=broken_rules)))

    assert decision.status is RiskDecisionStatus.REJECTED
    assert RiskReason.INVALID_MARKET_RULES in decision.reasons


def test_risk_reducing_exit_is_approved_during_halt_and_stale_snapshots() -> None:
    open_position = PositionRiskSnapshot(
        SYMBOL,
        "long",
        quantity=2,
        entry_price=100,
        mark_price=95,
        initial_risk=50,
        maintenance_margin_rate=0.005,
        correlation_group="majors",
        stop_price=90,
    )
    stale_time = 1
    decision = evaluate_risk(
        context(
            strategy_intent=intent(StrategyAction.EXIT),
            account_snapshot=account(
                positions=(open_position,),
                observed_at=stale_time,
                global_halt=True,
            ),
            market_snapshot=market(mark_price=95, observed_at=stale_time),
            evaluated_at=DECISION_TIME + 1,
        )
    )

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_action is RiskApprovedAction.CLOSE_LONG
    assert decision.reduce_only is True
    assert decision.quantity == open_position.quantity
    assert decision.required_margin == 0
    assert decision.portfolio_risk_after == 0
    assert "market_snapshot_stale_but_exit_allowed" in decision.evidence["warnings"]
