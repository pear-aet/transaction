from __future__ import annotations

from dataclasses import dataclass

from .domain import (
    Candle,
    ChanPoint,
    Direction,
    Divergence,
    Fractal,
    FractalKind,
    Pivot,
    Segment,
    Stroke,
)


@dataclass(frozen=True, slots=True)
class ChanSnapshot:
    candles: tuple[Candle, ...]
    fractals: tuple[Fractal, ...]
    strokes: tuple[Stroke, ...]
    pivots: tuple[Pivot, ...]
    segments: tuple[Segment, ...]
    divergences: tuple[Divergence, ...]
    points: tuple[ChanPoint, ...]
    trend: Direction


def _contains(a: Candle, b: Candle) -> bool:
    return (a.high >= b.high and a.low <= b.low) or (b.high >= a.high and b.low <= a.low)


def remove_inclusions(candles: list[Candle]) -> list[Candle]:
    """Merge containing bars with a deterministic directional rule.

    In an up move both bounds move upward; in a down move both move downward.
    The merged bar keeps the newest timestamp/close so it is never known early.
    """
    if len(candles) < 2:
        return list(candles)
    result: list[Candle] = []
    direction = Direction.FLAT
    for candle in candles:
        if not result or not _contains(result[-1], candle):
            if result:
                if candle.high > result[-1].high and candle.low > result[-1].low:
                    direction = Direction.UP
                elif candle.high < result[-1].high and candle.low < result[-1].low:
                    direction = Direction.DOWN
            result.append(candle)
            continue

        previous = result[-1]
        if direction is Direction.FLAT and len(result) >= 2:
            direction = Direction.UP if previous.close >= result[-2].close else Direction.DOWN
        if direction is Direction.DOWN:
            high, low = min(previous.high, candle.high), min(previous.low, candle.low)
        else:
            high, low = max(previous.high, candle.high), max(previous.low, candle.low)
        open_price = min(max(previous.open, low), high)
        close_price = min(max(candle.close, low), high)
        result[-1] = Candle(
            timestamp=candle.timestamp,
            open=open_price,
            high=high,
            low=low,
            close=close_price,
            volume=previous.volume + candle.volume,
        )
    return result


def find_fractals(candles: list[Candle]) -> list[Fractal]:
    """Find strict three-bar fractals; center bar is confirmed by the right bar."""
    found: list[Fractal] = []
    for index in range(1, len(candles) - 1):
        left, center, right = candles[index - 1 : index + 2]
        is_top = (
            center.high > left.high
            and center.high > right.high
            and center.low > left.low
            and center.low > right.low
        )
        is_bottom = (
            center.low < left.low
            and center.low < right.low
            and center.high < left.high
            and center.high < right.high
        )
        if is_top:
            found.append(Fractal(index, center.timestamp, FractalKind.TOP, center.high, center.low))
        elif is_bottom:
            found.append(
                Fractal(index, center.timestamp, FractalKind.BOTTOM, center.high, center.low)
            )
    return found


def build_strokes(fractals: list[Fractal], min_separation: int = 4) -> list[Stroke]:
    """Connect alternating fractals separated by enough normalized bars.

    Same-kind candidates replace the prior extreme. This explicit rule avoids
    discretionary redrawing and remains stable once the endpoint is confirmed.
    """
    if min_separation < 1:
        raise ValueError("min_separation must be positive")
    selected: list[Fractal] = []
    for fractal in fractals:
        if not selected:
            selected.append(fractal)
            continue
        last = selected[-1]
        if fractal.kind is last.kind:
            more_extreme = (
                fractal.high >= last.high
                if fractal.kind is FractalKind.TOP
                else fractal.low <= last.low
            )
            if more_extreme:
                selected[-1] = fractal
            continue
        if fractal.index - last.index < min_separation:
            continue
        valid = (
            fractal.high > last.high if fractal.kind is FractalKind.TOP else fractal.low < last.low
        )
        if valid:
            selected.append(fractal)
    return [Stroke(a, b) for a, b in zip(selected, selected[1:], strict=False)]


def find_pivots(strokes: list[Stroke]) -> list[Pivot]:
    """Find overlap of three adjacent stroke price ranges (a deterministic pivot)."""
    pivots: list[Pivot] = []
    for index in range(len(strokes) - 2):
        window = strokes[index : index + 3]
        lower = max(stroke.low for stroke in window)
        upper = min(stroke.high for stroke in window)
        if lower >= upper:
            continue
        if (
            pivots
            and index <= pivots[-1].end_index
            and not (upper < pivots[-1].lower or lower > pivots[-1].upper)
        ):
            prior = pivots[-1]
            merged_lower = max(prior.lower, lower)
            merged_upper = min(prior.upper, upper)
            if merged_lower < merged_upper:
                pivots[-1] = Pivot(prior.start_index, index + 2, merged_lower, merged_upper)
                continue
        pivots.append(Pivot(index, index + 2, lower, upper))
    return pivots


def build_segments(strokes: list[Stroke]) -> list[Segment]:
    """Build confirmed segments from a three-stroke directional break.

    This is a deliberately fixed research definition, not a claim that every
    discretionary Chan school uses the same segment rule.
    """
    output: list[Segment] = []
    start = 0
    for index in range(2, len(strokes)):
        first, third = strokes[index - 2], strokes[index]
        if first.direction is not third.direction:
            continue
        breakout = (
            third.end.high > first.end.high
            if third.direction is Direction.UP
            else third.end.low < first.end.low
        )
        if not breakout:
            continue
        window = strokes[start : index + 1]
        output.append(
            Segment(
                start,
                index,
                third.direction,
                max(item.high for item in window),
                min(item.low for item in window),
            )
        )
        start = index
    return output


def find_divergences(strokes: list[Stroke], pivots: list[Pivot]) -> list[Divergence]:
    """Compare speed of consecutive same-direction strokes after a pivot."""
    output: list[Divergence] = []
    if not pivots:
        return output
    for index in range(2, len(strokes)):
        previous, current = strokes[index - 2], strokes[index]
        if previous.direction is not current.direction:
            continue
        previous_duration = max(1, previous.end.index - previous.start.index)
        current_duration = max(1, current.end.index - current.start.index)
        previous_strength = (previous.high - previous.low) / previous_duration
        current_strength = (current.high - current.low) / current_duration
        new_extreme = (
            current.end.high > previous.end.high
            if current.direction is Direction.UP
            else current.end.low < previous.end.low
        )
        if new_extreme and previous_strength > 0 and current_strength < previous_strength * 0.8:
            output.append(
                Divergence(index, current.direction, current_strength / previous_strength)
            )
    return output


def find_buy_sell_points(
    strokes: list[Stroke], pivots: list[Pivot], divergences: list[Divergence]
) -> list[ChanPoint]:
    """Emit deterministic type-1 and type-3 research points after confirmation."""
    points: list[ChanPoint] = []
    for divergence in divergences:
        stroke = strokes[divergence.stroke_index]
        side = "sell" if divergence.direction is Direction.UP else "buy"
        price = stroke.end.high if side == "sell" else stroke.end.low
        points.append(ChanPoint(divergence.stroke_index, side, 1, price, stroke.end.timestamp))
    for pivot in pivots:
        after = pivot.end_index + 1
        if after >= len(strokes):
            continue
        stroke = strokes[after]
        if stroke.direction is Direction.UP and stroke.end.low > pivot.upper:
            points.append(ChanPoint(after, "buy", 3, pivot.upper, stroke.end.timestamp))
        elif stroke.direction is Direction.DOWN and stroke.end.high < pivot.lower:
            points.append(ChanPoint(after, "sell", 3, pivot.lower, stroke.end.timestamp))
    return sorted(points, key=lambda item: (item.confirmed_at, item.point_type))


def classify_trend(
    candles: list[Candle], fractals: list[Fractal], pivots: list[Pivot]
) -> Direction:
    if not candles:
        return Direction.FLAT
    tops = [f for f in fractals if f.kind is FractalKind.TOP]
    bottoms = [f for f in fractals if f.kind is FractalKind.BOTTOM]
    higher_structure = (
        len(tops) >= 2
        and len(bottoms) >= 2
        and tops[-1].high > tops[-2].high
        and bottoms[-1].low > bottoms[-2].low
    )
    lower_structure = (
        len(tops) >= 2
        and len(bottoms) >= 2
        and tops[-1].high < tops[-2].high
        and bottoms[-1].low < bottoms[-2].low
    )
    if pivots and candles[-1].close > pivots[-1].upper and higher_structure:
        return Direction.UP
    if pivots and candles[-1].close < pivots[-1].lower and lower_structure:
        return Direction.DOWN
    if higher_structure:
        return Direction.UP
    if lower_structure:
        return Direction.DOWN
    return Direction.FLAT


def analyze(candles: list[Candle], min_stroke_separation: int = 4) -> ChanSnapshot:
    normalized = remove_inclusions(candles)
    fractals = find_fractals(normalized)
    strokes = build_strokes(fractals, min_stroke_separation)
    pivots = find_pivots(strokes)
    segments = build_segments(strokes)
    divergences = find_divergences(strokes, pivots)
    points = find_buy_sell_points(strokes, pivots, divergences)
    trend = classify_trend(normalized, fractals, pivots)
    return ChanSnapshot(
        tuple(normalized),
        tuple(fractals),
        tuple(strokes),
        tuple(pivots),
        tuple(segments),
        tuple(divergences),
        tuple(points),
        trend,
    )
