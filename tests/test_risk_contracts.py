from dataclasses import replace

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    PositionRiskSnapshot,
    RiskContext,
    RiskDecision,
    RiskPolicy,
    risk_policy_hash,
)
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent


def rules() -> MarketRules:
    return MarketRules(
        "BTC/USDT:USDT",
        tick_size=0.1,
        step_size=0.001,
        min_quantity=0.001,
        min_notional=5,
        max_leverage=100,
        brackets=(RiskBracket(0, 1_000_000, 0.005),),
    )


def test_policy_hash_ignores_nothing_and_config_conversion_is_stable() -> None:
    policy = RiskPolicy()
    changed = replace(policy, risk_per_trade=0.005)
    assert risk_policy_hash(policy) != risk_policy_hash(changed)
    assert risk_policy_hash(policy) == risk_policy_hash(RiskPolicy())


def test_account_snapshot_rejects_duplicate_symbol_positions() -> None:
    position = PositionRiskSnapshot(
        "BTC/USDT:USDT",
        "long",
        1,
        100,
        100,
        10,
        0.005,
    )
    with pytest.raises(ValueError, match="duplicate symbol"):
        AccountRiskSnapshot(
            "snapshot",
            "account",
            1,
            10_000,
            10_000,
            1,
            10_000,
            10_000,
            0,
            (position, position),
        )


def test_risk_decision_round_trip_preserves_storage_metadata() -> None:
    strategy_intent = StrategyIntent(
        intent_id="intent-1",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol="BTC/USDT:USDT",
        action=StrategyAction.ENTER_LONG,
        decision_time=900_000,
        created_at=900_001,
        reference_price=100,
        stop_price=99,
        reason="test",
        source_event_id="market-1",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="hash",
        evidence={},
        storage_sequence=3,
    )
    account = AccountRiskSnapshot(
        "account-1",
        "paper",
        900_000,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    market = MarketRiskSnapshot(
        "market-1",
        "BTC/USDT:USDT",
        900_000,
        100,
        rules(),
    )
    decision = (
        evaluate_risk(
            RiskContext(
                "risk-primary",
                strategy_intent,
                account,
                market,
                RiskPolicy(),
                900_001,
            )
        )
        .with_sequence(4)
        .with_storage_sequence(5)
    )

    assert RiskDecision.from_dict(decision.to_dict()) == decision
    assert decision.to_dict()["sequence"] == 4
    assert decision.to_dict()["storage_sequence"] == 5


def test_contracts_reject_non_finite_values() -> None:
    with pytest.raises(ValueError, match="mark_price"):
        MarketRiskSnapshot(
            "market",
            "BTC/USDT:USDT",
            1,
            float("nan"),
            rules(),
        )
    with pytest.raises(ValueError, match="volatility"):
        MarketRiskSnapshot(
            "market",
            "BTC/USDT:USDT",
            1,
            100,
            rules(),
            volatility_annualized=float("inf"),
        )
