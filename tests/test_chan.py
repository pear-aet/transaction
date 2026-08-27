from crypto_quant.chan import analyze, find_fractals, remove_inclusions
from crypto_quant.domain import Candle, FractalKind


def candle(index: int, high: float, low: float) -> Candle:
    middle = (high + low) / 2
    return Candle(index * 60_000, middle, high, low, middle)


def test_inclusion_uses_new_timestamp_and_directional_bounds() -> None:
    values = [candle(0, 10, 5), candle(1, 12, 7), candle(2, 11, 8)]
    normalized = remove_inclusions(values)
    assert len(normalized) == 2
    assert normalized[-1].timestamp == values[-1].timestamp
    assert normalized[-1].high == 12
    assert normalized[-1].low == 8


def test_fractal_requires_closed_right_bar() -> None:
    values = [candle(0, 10, 5), candle(1, 12, 7)]
    assert find_fractals(values) == []
    values.append(candle(2, 11, 6))
    fractals = find_fractals(values)
    assert len(fractals) == 1
    assert fractals[0].kind is FractalKind.TOP


def test_prefix_analysis_does_not_change_past_candle_input() -> None:
    values = [
        candle(0, 10, 5),
        candle(1, 12, 7),
        candle(2, 11, 6),
        candle(3, 9, 4),
        candle(4, 10, 5),
    ]
    prefix = analyze(values[:3])
    full = analyze(values)
    assert prefix.fractals[0] == full.fractals[0]
