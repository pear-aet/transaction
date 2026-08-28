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
NOW = 1_000_000


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


def strategy_intent(action: StrategyAction) -> StrategyIntent:
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
        reason="math_invariant_test",
        source_event_id=f"market-{action.value}",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=1,
    )


def market() -> MarketRiskSnapshot:
    return MarketRiskSnapshot(
        "market-snapshot",
        SYMBOL,
        NOW,
        100,
        rules(),
    )


def approved_entry():
    account = AccountRiskSnapshot(
        "account-entry",
        "paper",
        NOW,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    return evaluate_risk(
        RiskContext(
            "risk-primary",
            strategy_intent(StrategyAction.ENTER_LONG),
            account,
            market(),
            RiskPolicy(),
            NOW,
        )
    )


def approved_exit():
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
        "account-exit",
        "paper",
        NOW,
        10_000,
        9_000,
        position.notional * position.maintenance_margin_rate,
        10_000,
        10_000,
        0,
        positions=(position,),
    )
    return evaluate_risk(
        RiskContext(
            "risk-primary",
            strategy_intent(StrategyAction.EXIT),
            account,
            market(),
            RiskPolicy(),
            NOW,
        )
    )


def test_approved_entry_has_a_single_approval_reason() -> None:
    decision = approved_entry()
    assert decision.status is RiskDecisionStatus.APPROVED

    with pytest.raises(ValueError, match="only the approved_entry"):
        replace(
            decision,
            reasons=(RiskReason.APPROVED_ENTRY, RiskReason.REFERENCE_DEVIATION),
        )


def test_approved_entry_stop_geometry_is_enforced() -> None:
    decision = approved_entry()

    with pytest.raises(ValueError, match="long stop"):
        replace(decision, stop_price=decision.approval_price)


def test_approved_notional_must_equal_quantity_times_price() -> None:
    decision = approved_entry()

    with pytest.raises(ValueError, match="notional must equal"):
        replace(decision, notional=decision.notional + 1)


def test_approved_margin_cannot_understate_notional_over_leverage() -> None:
    decision = approved_entry()
    understated = decision.notional / decision.leverage - 0.01
    assert understated > 0

    with pytest.raises(ValueError, match="margin is below"):
        replace(decision, required_margin=understated)


def test_approved_initial_risk_cannot_exceed_budget() -> None:
    decision = approved_entry()

    with pytest.raises(ValueError, match="initial risk exceeds"):
        replace(decision, initial_risk=decision.risk_budget + 1)


def test_approved_after_values_must_include_new_risk_and_notional() -> None:
    decision = approved_entry()

    with pytest.raises(ValueError, match="risk-after"):
        replace(decision, symbol_risk_after=decision.initial_risk - 1)
    with pytest.raises(ValueError, match="notional-after"):
        replace(decision, gross_notional_after=decision.notional - 1)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("leverage", 2.0, "cannot add leverage"),
        ("risk_budget", 1.0, "cannot add leverage"),
        ("initial_risk", 1.0, "cannot add leverage"),
    ],
)
def test_approved_exit_cannot_add_leverage_or_new_risk(
    field: str,
    value: float,
    message: str,
) -> None:
    decision = approved_exit()
    assert decision.status is RiskDecisionStatus.APPROVED

    with pytest.raises(ValueError, match=message):
        replace(decision, **{field: value})
