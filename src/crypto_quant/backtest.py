from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from statistics import fmean, pstdev
from typing import Any

from .config import StrategyConfig
from .data import timeframe_ms
from .domain import Candle, FundingRate, MarketRules, finite_json
from .engine import (
    EngineState,
    EventEngine,
    Position,
    Trade,
    max_drawdown,
    safe_profit_factor,
)
from .strategy import decide

__all__ = [
    "BacktestResult",
    "Position",
    "Trade",
    "run_backtest",
    "run_portfolio_backtest",
]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    initial_cash: float
    final_equity: float
    total_return: float
    max_drawdown: float
    sharpe: float
    trades: tuple[Trade, ...]
    win_rate: float
    profit_factor: float | None
    funding_source: str
    equity_curve: tuple[tuple[int, float], ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                **asdict(self),
                "trades": [asdict(trade) for trade in self.trades],
                "equity_curve": [list(point) for point in self.equity_curve],
            }
        )


def _result(
    state: EngineState,
    config: StrategyConfig,
    low_ms: int,
    funding_source: str,
) -> BacktestResult:
    equities = [value for _, value in state.equity_curve]
    returns = [b / a - 1 for a, b in zip(equities, equities[1:], strict=False) if a > 0]
    periods_per_year = 365 * 24 * 3_600_000 / low_ms
    sharpe = 0.0
    if len(returns) > 1 and pstdev(returns) > 0:
        sharpe = fmean(returns) / pstdev(returns) * math.sqrt(periods_per_year)
    wins = [trade for trade in state.trades if trade.net_pnl > 0]
    return BacktestResult(
        config.initial_cash,
        state.cash,
        state.cash / config.initial_cash - 1,
        max_drawdown(state.equity_curve),
        sharpe,
        tuple(state.trades),
        len(wins) / len(state.trades) if state.trades else 0.0,
        safe_profit_factor(state.trades),
        funding_source,
        tuple(state.equity_curve),
        tuple(sorted(state.warnings)),
    )


def run_backtest(
    low_candles: list[Candle],
    high_candles: list[Candle],
    config: StrategyConfig,
    funding_rates: list[FundingRate] | None = None,
    mark_candles: list[Candle] | None = None,
    market_rules: MarketRules | None = None,
) -> BacktestResult:
    """Run a symbol through the same event engine used by paper trading."""
    if len(low_candles) < 50 or len(high_candles) < 20:
        raise ValueError("not enough data: need >=50 low-TF and >=20 high-TF candles")
    low_ms, high_ms = timeframe_ms(config.low_timeframe), timeframe_ms(config.high_timeframe)
    rules = {config.symbol: market_rules} if market_rules else None
    engine = EventEngine(config, market_rules=rules, decision_function=decide)
    marks = {item.timestamp: item for item in mark_candles or []}
    if config.require_mark_price and not mark_candles:
        engine.state.warnings.add(
            "Mark-price candles were unavailable; liquidation used contract-price candles."
        )
    rates = sorted(funding_rates or [], key=lambda item: item.timestamp)
    for index, bar in enumerate(low_candles):
        close_time = bar.timestamp + low_ms
        closed_high = [item for item in high_candles if item.timestamp + high_ms <= close_time]
        engine.process_bar(
            config.symbol,
            bar,
            low_candles[: index + 1],
            closed_high,
            rates,
            marks.get(bar.timestamp),
        )
    engine.close_all(low_candles[-1].timestamp + low_ms)
    return _result(
        engine.state,
        config,
        low_ms,
        "exchange_history" if rates else "configured_fallback",
    )


def run_portfolio_backtest(
    low_by_symbol: dict[str, list[Candle]],
    high_by_symbol: dict[str, list[Candle]],
    configs: dict[str, StrategyConfig],
    funding_by_symbol: dict[str, list[FundingRate]] | None = None,
    marks_by_symbol: dict[str, list[Candle]] | None = None,
    rules_by_symbol: dict[str, MarketRules] | None = None,
) -> BacktestResult:
    """Chronologically process a whitelist through one cross-margin account."""
    if not configs or set(configs) != set(low_by_symbol):
        raise ValueError("configs and low_by_symbol must contain the same symbols")
    base = next(iter(configs.values()))
    engine = EventEngine(base, market_rules=rules_by_symbol, decision_function=decide)
    funding_map = funding_by_symbol or {}
    mark_maps = {
        symbol: {item.timestamp: item for item in candles}
        for symbol, candles in (marks_by_symbol or {}).items()
    }
    timeline = sorted(
        (bar.timestamp, symbol, index, bar)
        for symbol, candles in low_by_symbol.items()
        for index, bar in enumerate(candles)
    )
    if not timeline:
        raise ValueError("portfolio backtest has no candles")
    for _, symbol, index, bar in timeline:
        config = configs[symbol]
        low = low_by_symbol[symbol]
        high = high_by_symbol.get(symbol, [])
        low_ms = timeframe_ms(config.low_timeframe)
        high_ms = timeframe_ms(config.high_timeframe)
        close_time = bar.timestamp + low_ms
        closed_high = [item for item in high if item.timestamp + high_ms <= close_time]
        engine.process_bar(
            symbol,
            bar,
            low[: index + 1],
            closed_high,
            funding_map.get(symbol),
            mark_maps.get(symbol, {}).get(bar.timestamp),
            strategy_config=config,
        )
    end = max(
        item.timestamp + timeframe_ms(configs[symbol].low_timeframe)
        for symbol, candles in low_by_symbol.items()
        for item in candles[-1:]
    )
    engine.close_all(end)
    return _result(
        engine.state,
        base,
        timeframe_ms(base.low_timeframe),
        "exchange_history" if funding_map else "configured_fallback",
    )
