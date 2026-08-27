from dataclasses import replace

import pytest

from crypto_quant.chan import ChanSnapshot
from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle, Direction, Signal
from crypto_quant.market_events import candle_market_event
from crypto_quant.strategy import StrategyDecision, strategy_spec
from crypto_quant.strategy_contracts import (
    StrategyAction,
    StrategyIntent,
    StrategyIntentFilter,
    strategy_intent_from_signal,
    strategy_parameter_hash,
)


def empty_snapshot() -> ChanSnapshot:
    return ChanSnapshot((), (), (), (), (), (), (), Direction.FLAT)


def config() -> StrategyConfig:
    return StrategyConfig(
        symbol="BTC/USDT:USDT",
        high_timeframe="1h",
        low_timeframe="15m",
        entry_model="chan_fractal",
        entry_lookback=20,
    )


def source_event():
    return candle_market_event(
        "BTC/USDT:USDT",
        "15m",
        Candle(0, 100, 102, 99, 101, 10),
        received_at=900_001,
    ).with_sequence(7)


def test_intent_identity_is_deterministic_and_excludes_delivery_metadata() -> None:
    decision = StrategyDecision(
        Signal(0, "enter_long", "test_entry", 99),
        empty_snapshot(),
        empty_snapshot(),
    )
    first = strategy_intent_from_signal(
        decision.signal,
        decision,
        config(),
        strategy_spec("chan_fractal"),
        source_event(),
        instance_id="primary",
        created_at=1_000_000,
    )
    second = strategy_intent_from_signal(
        decision.signal,
        decision,
        config(),
        strategy_spec("chan_fractal"),
        source_event(),
        instance_id="primary",
        created_at=2_000_000,
    )

    assert first.intent_id == second.intent_id
    assert first.semantic_dict() == second.semantic_dict()
    assert first.created_at != second.created_at
    assert first.action is StrategyAction.ENTER_LONG
    assert first.decision_time == 900_000
    assert first.reference_price == 101
    assert first.source_sequence == 7
    assert "quantity" not in first.to_dict()
    assert "leverage" not in first.to_dict()
    sequenced = first.with_sequence(3)
    assert StrategyIntent.from_dict(sequenced.to_dict()) == sequenced


def test_parameter_hash_ignores_risk_sizing_but_tracks_strategy_rules() -> None:
    base = config()
    risk_changed = replace(base, risk_per_trade=0.02, max_leverage=5)
    rule_changed = replace(base, entry_lookback=30)
    assert strategy_parameter_hash(base) == strategy_parameter_hash(risk_changed)
    assert strategy_parameter_hash(base) != strategy_parameter_hash(rule_changed)


def test_entry_stop_geometry_and_filters_are_strict() -> None:
    decision = StrategyDecision(
        Signal(0, "enter_long", "bad", 102),
        empty_snapshot(),
        empty_snapshot(),
    )
    with pytest.raises(ValueError, match="stop below"):
        strategy_intent_from_signal(
            decision.signal,
            decision,
            config(),
            strategy_spec("chan_fractal"),
            source_event(),
            instance_id="primary",
            created_at=1,
        )

    valid_decision = StrategyDecision(
        Signal(0, "exit", "trend_reversal"),
        empty_snapshot(),
        empty_snapshot(),
    )
    intent = strategy_intent_from_signal(
        valid_decision.signal,
        valid_decision,
        config(),
        strategy_spec("chan_fractal"),
        source_event(),
        instance_id="primary",
        created_at=1,
    )
    matching = StrategyIntentFilter(
        strategy_ids=frozenset({"chan_fractal"}),
        symbols=frozenset({"BTC/USDT:USDT"}),
        actions=frozenset({StrategyAction.EXIT}),
    )
    assert matching.matches(intent)
    assert not StrategyIntentFilter(actions=frozenset({StrategyAction.ENTER_LONG})).matches(intent)
