from dataclasses import replace

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    PositionRiskSnapshot,
    RiskContext,
    RiskDecisionStatus,
    RiskPolicy,
    RiskReason,
)
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
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


def intent(
    action: StrategyAction,
    *,
    decision_time: int = 900_000,
) -> StrategyIntent:
    return StrategyIntent(
        intent_id=f"intent-{action.value}-{decision_time}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=action,
        decision_time=decision_time,
        created_at=decision_time,
        reference_price=100,
        stop_price=99 if action is StrategyAction.ENTER_LONG else None,
        reason="validity_test",
        source_event_id=f"market-{decision_time}",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="hash",
        evidence={},
        storage_sequence=1,
    )


def account(*, position: PositionRiskSnapshot | None = None) -> AccountRiskSnapshot:
    positions = () if position is None else (position,)
    return AccountRiskSnapshot(
        "account-snapshot",
        "paper",
        900_000,
        10_000,
        10_000,
        0 if position is None else position.notional * position.maintenance_margin_rate,
        10_000,
        10_000,
        0,
        positions,
    )


def market() -> MarketRiskSnapshot:
    return MarketRiskSnapshot(
        "market-snapshot",
        SYMBOL,
        900_000,
        100,
        rules(),
    )


def context(strategy_intent: StrategyIntent, *, position=None, policy=None) -> RiskContext:
    return RiskContext(
        "risk-primary",
        strategy_intent,
        account(position=position),
        market(),
        policy or RiskPolicy(),
        NOW,
    )


def test_entry_validity_is_bounded_by_source_snapshot_freshness() -> None:
    policy = replace(
        RiskPolicy(),
        max_intent_age_ms=1_800_000,
        max_account_age_ms=30_000,
        max_market_age_ms=20_000,
    )
    decision = evaluate_risk(context(intent(StrategyAction.ENTER_LONG), policy=policy))

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.valid_until == 920_000
    assert decision.valid_until >= decision.evaluated_at


def test_rejected_decision_expires_at_evaluation_time() -> None:
    decision = evaluate_risk(
        RiskContext(
            "risk-primary",
            intent(StrategyAction.ENTER_LONG),
            account(),
            replace(market(), mark_price=110),
            replace(RiskPolicy(), max_reference_deviation=0.01),
            NOW,
        )
    )

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.valid_until == decision.evaluated_at


def test_future_entry_and_exit_intents_fail_closed() -> None:
    future_time = NOW + 1
    future_entry = evaluate_risk(context(intent(StrategyAction.ENTER_LONG, decision_time=future_time)))
    position = PositionRiskSnapshot(
        SYMBOL,
        "long",
        1,
        100,
        100,
        10,
        0.005,
    )
    future_exit = evaluate_risk(
        context(intent(StrategyAction.EXIT, decision_time=future_time), position=position)
    )

    assert future_entry.status is RiskDecisionStatus.REJECTED
    assert RiskReason.FUTURE_INTENT in future_entry.reasons
    assert future_exit.status is RiskDecisionStatus.REJECTED
    assert RiskReason.FUTURE_INTENT in future_exit.reasons


def test_contract_rejects_expiry_before_evaluation() -> None:
    decision = evaluate_risk(context(intent(StrategyAction.ENTER_LONG)))
    with pytest.raises(ValueError, match="expire before evaluation"):
        replace(decision, valid_until=decision.evaluated_at - 1)
