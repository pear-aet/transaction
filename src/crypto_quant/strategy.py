from __future__ import annotations

from dataclasses import dataclass

from .chan import ChanSnapshot, analyze
from .config import StrategyConfig
from .domain import Candle, Direction, Fractal, FractalKind, Signal


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    signal: Signal | None
    high_snapshot: ChanSnapshot
    low_snapshot: ChanSnapshot


@dataclass(frozen=True, slots=True)
class StrategySpec:
    strategy_id: str
    version: str
    source: str
    license: str
    description: str
    parameter_grid: tuple[int, ...]


STRATEGIES: dict[str, StrategySpec] = {
    "chan_fractal": StrategySpec(
        "chan_fractal",
        "2.0.0",
        "local-formalized-chan",
        "MIT",
        "HTF Chan structure with a confirmed LTF three-bar fractal",
        (20,),
    ),
    "chan_third_point": StrategySpec(
        "chan_third_point",
        "2.0.0",
        "local-formalized-chan",
        "MIT",
        "HTF Chan structure with a confirmed LTF type-3 pivot departure",
        (20,),
    ),
    "donchian": StrategySpec(
        "donchian",
        "2.0.0",
        "Richard Donchian / local implementation",
        "MIT",
        "HTF structure-filtered Donchian close breakout",
        (20, 55),
    ),
    "ema_pullback": StrategySpec(
        "ema_pullback",
        "2.0.0",
        "public-domain concept / local implementation",
        "MIT",
        "HTF structure-filtered EMA reclaim or rejection",
        (20, 50),
    ),
}


def strategy_registry() -> tuple[StrategySpec, ...]:
    return tuple(STRATEGIES.values())


def _latest_bottom(snapshot: ChanSnapshot) -> Fractal | None:
    return next((f for f in reversed(snapshot.fractals) if f.kind is FractalKind.BOTTOM), None)


def _latest_top(snapshot: ChanSnapshot) -> Fractal | None:
    return next((f for f in reversed(snapshot.fractals) if f.kind is FractalKind.TOP), None)


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def _structural_stop(low: ChanSnapshot, side: str, buffer: float, lookback: int) -> float:
    if side == "long":
        bottom = _latest_bottom(low)
        base = bottom.low if bottom else min(candle.low for candle in low.candles[-lookback:])
        return base * (1 - buffer)
    top = _latest_top(low)
    base = top.high if top else max(candle.high for candle in low.candles[-lookback:])
    return base * (1 + buffer)


def decide(
    low_candles: list[Candle],
    high_candles: list[Candle],
    config: StrategyConfig,
    position_side: str | None = None,
) -> StrategyDecision:
    high = analyze(high_candles, config.min_stroke_separation)
    low = analyze(low_candles, config.min_stroke_separation)
    signal: Signal | None = None
    if len(low.candles) < 3:
        return StrategyDecision(signal, high, low)

    if position_side == "long" and high.trend is Direction.DOWN:
        signal = Signal(low.candles[-1].timestamp, "exit", "high_timeframe_reversal")
    elif position_side == "short" and high.trend is Direction.UP:
        signal = Signal(low.candles[-1].timestamp, "exit", "high_timeframe_reversal")
    elif (
        position_side is None
        and config.entry_model == "chan_fractal"
        and high.trend is Direction.UP
    ):
        bottom = _latest_bottom(low)
        right_bar = low.candles[-1]
        # The bottom at N-1 is only confirmed after N closes. Entry occurs next open.
        if (
            bottom is not None
            and bottom.index == len(low.candles) - 2
            and right_bar.close > right_bar.open
            and right_bar.high > bottom.high
        ):
            stop = bottom.low * (1 - config.stop_buffer)
            signal = Signal(right_bar.timestamp, "enter_long", "htf_up_ltf_confirmed_bottom", stop)
    elif (
        position_side is None
        and config.entry_model == "chan_fractal"
        and high.trend is Direction.DOWN
    ):
        top = _latest_top(low)
        right_bar = low.candles[-1]
        if (
            top is not None
            and top.index == len(low.candles) - 2
            and right_bar.close < right_bar.open
            and right_bar.low < top.low
        ):
            stop = top.high * (1 + config.stop_buffer)
            signal = Signal(right_bar.timestamp, "enter_short", "htf_down_ltf_confirmed_top", stop)
    elif position_side is None and config.entry_model == "chan_third_point":
        latest = low.points[-1] if low.points else None
        current = low.candles[-1]
        if (
            latest
            and latest.confirmed_at == current.timestamp
            and latest.side == "buy"
            and latest.point_type == 3
            and high.trend is Direction.UP
        ):
            stop = _structural_stop(low, "long", config.stop_buffer, config.entry_lookback)
            signal = Signal(current.timestamp, "enter_long", "htf_up_chan_type3_buy", stop)
        elif (
            latest
            and latest.confirmed_at == current.timestamp
            and latest.side == "sell"
            and latest.point_type == 3
            and high.trend is Direction.DOWN
        ):
            stop = _structural_stop(low, "short", config.stop_buffer, config.entry_lookback)
            signal = Signal(current.timestamp, "enter_short", "htf_down_chan_type3_sell", stop)
    elif (
        position_side is None
        and config.entry_model == "donchian"
        and len(low.candles) > config.entry_lookback
    ):
        current = low.candles[-1]
        prior = low.candles[-config.entry_lookback - 1 : -1]
        if high.trend is Direction.UP and current.close > max(candle.high for candle in prior):
            stop = _structural_stop(low, "long", config.stop_buffer, config.entry_lookback)
            signal = Signal(current.timestamp, "enter_long", "htf_up_donchian_breakout", stop)
        elif high.trend is Direction.DOWN and current.close < min(candle.low for candle in prior):
            stop = _structural_stop(low, "short", config.stop_buffer, config.entry_lookback)
            signal = Signal(current.timestamp, "enter_short", "htf_down_donchian_breakout", stop)
    elif (
        position_side is None
        and config.entry_model == "ema_pullback"
        and len(low.candles) > config.entry_lookback
    ):
        closes = [candle.close for candle in low.candles]
        ema = _ema(closes, config.entry_lookback)
        current, previous = low.candles[-1], low.candles[-2]
        if high.trend is Direction.UP and previous.close <= ema[-2] and current.close > ema[-1]:
            stop = _structural_stop(low, "long", config.stop_buffer, config.entry_lookback)
            signal = Signal(current.timestamp, "enter_long", "htf_up_ema_reclaim", stop)
        elif high.trend is Direction.DOWN and previous.close >= ema[-2] and current.close < ema[-1]:
            stop = _structural_stop(low, "short", config.stop_buffer, config.entry_lookback)
            signal = Signal(current.timestamp, "enter_short", "htf_down_ema_reject", stop)
    return StrategyDecision(signal, high, low)
