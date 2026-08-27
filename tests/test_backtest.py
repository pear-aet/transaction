from types import SimpleNamespace

import pytest

from crypto_quant.backtest import run_backtest
from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle, Signal

START = 80_000_000


def bars(count: int, special: dict[int, tuple[float, float]] | None = None) -> list[Candle]:
    output = []
    for index in range(count):
        high, low = (special or {}).get(index, (100.5, 99.5))
        output.append(Candle(START + index * 900_000, 100, high, low, 100, 10))
    return output


def high_bars() -> list[Candle]:
    return [Candle(index * 3_600_000, 100, 101, 99, 100, 10) for index in range(30)]


def config() -> StrategyConfig:
    return StrategyConfig(
        high_timeframe="1h",
        low_timeframe="15m",
        fee_rate=0,
        slippage_rate=0,
        fallback_funding_rate_8h=0,
    )


def test_next_open_execution_and_take_profit(monkeypatch: pytest.MonkeyPatch) -> None:
    low = bars(60, {11: (103, 99.5)})
    signal_time = low[10].timestamp

    def fake_decide(low_candles, high_candles, cfg, position_side):
        signal = (
            Signal(signal_time, "enter_long", "test", 99)
            if low_candles[-1].timestamp == signal_time and position_side is None
            else None
        )
        return SimpleNamespace(signal=signal)

    monkeypatch.setattr("crypto_quant.backtest.decide", fake_decide)
    result = run_backtest(low, high_bars(), config())
    assert len(result.trades) == 1
    assert result.trades[0].entry_time == low[11].timestamp
    assert result.trades[0].exit_reason == "take_profit"
    assert result.final_equity > result.initial_cash


def test_stop_precedes_target_when_same_bar_is_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    low = bars(60, {11: (103, 98.5)})
    signal_time = low[10].timestamp

    def fake_decide(low_candles, high_candles, cfg, position_side):
        signal = (
            Signal(signal_time, "enter_long", "test", 99)
            if low_candles[-1].timestamp == signal_time and position_side is None
            else None
        )
        return SimpleNamespace(signal=signal)

    monkeypatch.setattr("crypto_quant.backtest.decide", fake_decide)
    result = run_backtest(low, high_bars(), config())
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.final_equity < result.initial_cash


def test_high_effective_leverage_warns_on_coarse_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    low = bars(60)
    signal_time = low[10].timestamp

    def fake_decide(low_candles, high_candles, cfg, position_side):
        signal = None
        if low_candles[-1].timestamp == signal_time and position_side is None:
            signal = Signal(signal_time, "enter_long", "test", 99.999)
        return SimpleNamespace(signal=signal)

    monkeypatch.setattr("crypto_quant.backtest.decide", fake_decide)
    result = run_backtest(low, high_bars(), config())
    assert any("10x" in warning for warning in result.warnings)
