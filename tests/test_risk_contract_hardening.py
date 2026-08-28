from dataclasses import replace

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskApprovedAction,
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


def intent(action: StrategyAction = StrategyAction.ENTER_LONG) -> StrategyIntent:
    return StrategyIntent(
        intent_id=f"intent-{action.value}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=action,
        decision_time=900_000,
        created_at=900_000,
        reference_price=100,
        stop_price=99 if action is StrategyAction.ENTER_LONG else None,
        reason="contract_hardening",
        source_event_id=f"market-{action.value}",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="hash",
        evidence={},
        storage_sequence=1,
    )


def account() -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        "account-snapshot",
        "paper",
        900_000,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )


def market(mark_price: float = 100) -> MarketRiskSnapshot:
    return MarketRiskSnapshot(
        f"market-{mark_price}",
        SYMBOL,
        900_000,
        mark_price,
        rules(),
    )


def decision(mark_price: float = 100):
    return evaluate_risk(
        RiskContext(
            "risk-primary",
            intent(),
            account(),
            market(mark_price),
            RiskPolicy(),
            NOW,
        )
    )


def test_approved_action_must_match_requested_strategy_action() -> None:
    approved = decision()
    assert approved.status is RiskDecisionStatus.APPROVED

    with pytest.raises(ValueError, match="does not match"):
        replace(approved, approved_action=RiskApprovedAction.OPEN_SHORT)


def test_risk_decision_reasons_must_be_unique() -> None:
    approved = decision()

    with pytest.raises(ValueError, match="reasons must be unique"):
        replace(
            approved,
            reasons=(RiskReason.APPROVED_ENTRY, RiskReason.APPROVED_ENTRY),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("required_margin", 1.0),
        ("risk_budget", 1.0),
        ("initial_risk", 1.0),
    ],
)
def test_rejected_decision_cannot_retain_execution_sizing(field: str, value: float) -> None:
    rejected = decision(mark_price=110)
    assert rejected.status is RiskDecisionStatus.REJECTED

    with pytest.raises(ValueError, match="execution sizing"):
        replace(rejected, **{field: value})


def test_rejected_decision_cannot_claim_an_approval_reason() -> None:
    rejected = decision(mark_price=110)

    with pytest.raises(ValueError, match="approval reason"):
        replace(rejected, reasons=(RiskReason.APPROVED_ENTRY,))
