from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import StrategyConfig
from .data import timeframe_ms
from .domain import Candle, FundingRate, MarketRules
from .engine import EngineState, EventEngine

PaperState = EngineState


def load_state(path: Path, initial_cash: float) -> PaperState:
    if not path.exists():
        return EngineState(initial_cash)
    data = json.loads(path.read_text(encoding="utf-8"))
    if "cash" in data:
        return EngineState.from_dict(data)
    # One-time migration from the v0.1 single-symbol JSON format.
    migrated = EngineState(float(data.get("balance", initial_cash)))
    migrated.events.append({"timestamp": 0, "kind": "state_migrated", "from_version": "0.1"})
    return migrated


def save_state(path: Path, state: PaperState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def advance_paper(
    state: PaperState,
    low_candles: list[Candle],
    high_candles: list[Candle],
    config: StrategyConfig,
    funding_rates: list[FundingRate] | None = None,
    mark_candles: list[Candle] | None = None,
    market_rules: MarketRules | None = None,
) -> PaperState:
    """Advance persistent paper state using the shared backtest event engine."""
    if not low_candles:
        return state
    symbol = config.symbol
    low_ms, high_ms = timeframe_ms(config.low_timeframe), timeframe_ms(config.high_timeframe)
    if symbol not in state.last_timestamps:
        state.last_timestamps[symbol] = (
            low_candles[-2].timestamp if len(low_candles) > 1 else low_candles[-1].timestamp - 1
        )
    rules = {symbol: market_rules} if market_rules else None
    engine = EventEngine(config, state, rules)
    marks = {item.timestamp: item for item in mark_candles or []}
    if config.require_mark_price and not mark_candles:
        state.warnings.add(
            "Mark-price candles were unavailable; liquidation used contract-price candles."
        )
    for index, bar in enumerate(low_candles):
        if bar.timestamp <= state.last_timestamps.get(symbol, -1):
            continue
        close_time = bar.timestamp + low_ms
        closed_high = [item for item in high_candles if item.timestamp + high_ms <= close_time]
        engine.process_bar(
            symbol,
            bar,
            low_candles[: index + 1],
            closed_high,
            funding_rates,
            marks.get(bar.timestamp),
        )
    return state


def paper_status(state: PaperState, latest_prices: float | dict[str, float]) -> dict[str, Any]:
    prices = latest_prices if isinstance(latest_prices, dict) else {}
    if isinstance(latest_prices, float) and len(state.positions) == 1:
        prices = {next(iter(state.positions)): latest_prices}
    engine_config = StrategyConfig(initial_cash=max(state.cash, 1.0))
    engine = EventEngine(engine_config, state)
    equity = engine.equity(prices)
    return {
        "cash": state.cash,
        "equity": equity,
        "positions": {key: asdict(value) for key, value in state.positions.items()},
        "pending": {key: asdict(value) for key, value in state.pending.items()},
        "last_timestamps": state.last_timestamps,
        "closed_trades": len(state.trades),
        "risk": {
            "day_paused": state.day_paused,
            "drawdown_halted": state.drawdown_halted,
            "global_stop": state.global_stop,
            "consecutive_losses": state.consecutive_losses,
        },
        "warnings": sorted(state.warnings),
    }
