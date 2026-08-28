from dataclasses import replace

import pytest

from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle
from crypto_quant.strategy import StrategyContext, decide, evaluate_strategy

LOW_MS = 900_000
HIGH_MS = 3_600_000


def candles(count: int, interval: int) -> list[Candle]:
    return [
        Candle(
            index * interval,
            100 + index,
            101 + index,
            99 + index,
            100.5 + index,
            10 + index,
        )
        for index in range(count)
    ]


def test_backtest_adapter_and_pure_strategy_entry_are_identical() -> None:
    low = candles(100, LOW_MS)
    high = candles(20, HIGH_MS)
    low_before = list(low)
    high_before = list(high)
    config = StrategyConfig(
        symbol="BTC/USDT:USDT",
        high_timeframe="1h",
        low_timeframe="15m",
        entry_model="donchian",
        entry_lookback=20,
    )
    decision_time = low[-1].timestamp + LOW_MS

    pure = evaluate_strategy(StrategyContext(tuple(low), tuple(high), config, decision_time, None))
    compatible = decide(low, high, config, None)
    repeated = evaluate_strategy(
        StrategyContext(tuple(low), tuple(high), config, decision_time, None)
    )

    assert pure == compatible == repeated
    assert low == low_before
    assert high == high_before


def test_risk_and_leverage_configuration_cannot_change_strategy_decision() -> None:
    low = candles(100, LOW_MS)
    high = candles(20, HIGH_MS)
    base = StrategyConfig(
        symbol="BTC/USDT:USDT",
        high_timeframe="1h",
        low_timeframe="15m",
        entry_model="ema_pullback",
        entry_lookback=20,
    )
    risk_changed = replace(
        base,
        risk_per_trade=0.02,
        max_leverage=5,
        max_symbol_risk=0.02,
        max_portfolio_risk=0.04,
    )
    decision_time = low[-1].timestamp + LOW_MS

    first = evaluate_strategy(StrategyContext(tuple(low), tuple(high), base, decision_time, None))
    second = evaluate_strategy(
        StrategyContext(tuple(low), tuple(high), risk_changed, decision_time, None)
    )
    assert first == second


def test_strategy_context_rejects_internal_candle_gaps() -> None:
    low = candles(100, LOW_MS)
    high = candles(20, HIGH_MS)
    gapped_low = tuple(low[:50] + low[51:])
    config = StrategyConfig(
        symbol="BTC/USDT:USDT",
        high_timeframe="1h",
        low_timeframe="15m",
        entry_model="chan_fractal",
    )
    decision_time = low[-1].timestamp + LOW_MS

    with pytest.raises(ValueError, match="contains a gap"):
        StrategyContext(gapped_low, tuple(high), config, decision_time, None)
