import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from crypto_quant.backtest import run_backtest
from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle, Signal
from crypto_quant.engine import EngineState, EventEngine, default_market_rules
from crypto_quant.paper import advance_paper

START = 80_000_000


def bars(count: int, special: dict[int, tuple[float, float]] | None = None) -> list[Candle]:
    output = []
    for index in range(count):
        high, low = (special or {}).get(index, (100.5, 99.5))
        output.append(Candle(START + index * 900_000, 100, high, low, 100, 10))
    return output


def high_bars() -> list[Candle]:
    return [Candle(index * 3_600_000, 100, 101, 99, 100, 10) for index in range(40)]


def config(symbol: str = "BTC/USDT:USDT") -> StrategyConfig:
    return StrategyConfig(
        symbol=symbol,
        high_timeframe="1h",
        low_timeframe="15m",
        fee_rate=0,
        slippage_rate=0,
        fallback_funding_rate_8h=0,
        require_mark_price=False,
    )


def test_backtest_and_paper_share_signal_timeline(monkeypatch: pytest.MonkeyPatch) -> None:
    low = bars(60, {11: (103, 99.5)})
    signal_time = low[10].timestamp

    def fake_decide(low_candles, high_candles, cfg, position_side):
        signal = None
        if low_candles[-1].timestamp == signal_time and position_side is None:
            signal = Signal(signal_time, "enter_long", "test", 99)
        return SimpleNamespace(signal=signal)

    monkeypatch.setattr("crypto_quant.backtest.decide", fake_decide)
    monkeypatch.setattr("crypto_quant.engine.decide", fake_decide)
    backtest = run_backtest(low, high_bars(), config())
    state = EngineState(10_000, last_timestamps={config().symbol: low[0].timestamp - 1})
    paper = advance_paper(state, low, high_bars(), config())
    assert len(backtest.trades) == len(paper.trades) == 1
    assert backtest.trades[0].entry_time == paper.trades[0].entry_time == low[11].timestamp
    assert backtest.trades[0].exit_reason == paper.trades[0].exit_reason == "take_profit"


def test_two_symbols_share_one_portfolio_risk_budget() -> None:
    first = "BTC/USDT:USDT"
    second = "ETH/USDT:USDT"
    cfg = replace(config(first), fee_rate=0.001, max_portfolio_risk=0.02, max_symbol_risk=0.015)
    state = EngineState(cfg.initial_cash)
    rules = {first: default_market_rules(cfg, first), second: default_market_rules(cfg, second)}
    engine = EventEngine(cfg, state, rules)
    bar = Candle(0, 100, 101, 99.5, 100, 1)
    for symbol in (first, second):
        state.pending[symbol] = Signal(0, "enter_long", "test", 99)
        engine.process_bar(symbol, bar, [bar], [], generate_signal=False)
    assert len(state.positions) == 2
    assert sum(item.initial_risk for item in state.positions.values()) <= 200.01
    assert state.cash < 10_000
    assert state.cash > 9_900


def test_backtest_result_is_strict_json() -> None:
    result = run_backtest(bars(60), high_bars(), config())
    encoded = json.dumps(result.to_dict(), allow_nan=False)
    assert "Infinity" not in encoded
    assert result.profit_factor == 0.0


def test_mark_price_cross_liquidation_closes_account() -> None:
    cfg = replace(config(), fee_rate=0.001)
    state = EngineState(cfg.initial_cash)
    state.pending[cfg.symbol] = Signal(0, "enter_long", "test", 99.9)
    engine = EventEngine(cfg, state)
    contract = Candle(0, 100, 101, 99.95, 100, 1)
    mark = Candle(0, 100, 101, 1, 50, 0)
    engine.process_bar(cfg.symbol, contract, [contract], [], mark_bar=mark, generate_signal=False)
    assert state.positions == {}
    assert state.global_stop is True
    assert state.trades[-1].exit_reason == "liquidation"
