from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp < 0:
            raise ValueError("timestamp must be non-negative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("invalid OHLC relationship")
        if self.low > self.high:
            raise ValueError("low cannot exceed high")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FractalKind(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"


@dataclass(frozen=True, slots=True)
class Fractal:
    index: int
    timestamp: int
    kind: FractalKind
    high: float
    low: float


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class Stroke:
    start: Fractal
    end: Fractal

    @property
    def direction(self) -> Direction:
        return Direction.UP if self.end.kind is FractalKind.TOP else Direction.DOWN

    @property
    def low(self) -> float:
        return min(self.start.low, self.end.low)

    @property
    def high(self) -> float:
        return max(self.start.high, self.end.high)


@dataclass(frozen=True, slots=True)
class Pivot:
    start_index: int
    end_index: int
    lower: float
    upper: float


@dataclass(frozen=True, slots=True)
class Segment:
    start_index: int
    end_index: int
    direction: Direction
    high: float
    low: float


@dataclass(frozen=True, slots=True)
class Divergence:
    stroke_index: int
    direction: Direction
    strength_ratio: float


@dataclass(frozen=True, slots=True)
class ChanPoint:
    stroke_index: int
    side: str
    point_type: int
    price: float
    confirmed_at: int


@dataclass(frozen=True, slots=True)
class Signal:
    timestamp: int
    action: str
    reason: str
    stop_price: float | None = None


@dataclass(frozen=True, slots=True)
class FundingRate:
    timestamp: int
    rate: float


@dataclass(frozen=True, slots=True)
class RiskBracket:
    notional_floor: float
    notional_cap: float
    maintenance_margin_rate: float
    maintenance_amount: float = 0.0

    def __post_init__(self) -> None:
        if not 0 <= self.notional_floor < self.notional_cap:
            raise ValueError("invalid risk bracket notional range")
        if not 0 < self.maintenance_margin_rate < 1:
            raise ValueError("invalid maintenance margin rate")


@dataclass(frozen=True, slots=True)
class MarketRules:
    symbol: str
    tick_size: float
    step_size: float
    min_quantity: float
    min_notional: float
    max_leverage: float
    brackets: tuple[RiskBracket, ...]

    def __post_init__(self) -> None:
        if min(self.tick_size, self.step_size, self.max_leverage) <= 0:
            raise ValueError("market increments and leverage must be positive")
        if self.min_quantity < 0 or self.min_notional < 0:
            raise ValueError("market minimums cannot be negative")
        if not self.brackets:
            raise ValueError("at least one risk bracket is required")


def finite_json(value: Any) -> Any:
    """Return a strict-JSON-safe structure without NaN or infinities."""
    import math

    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(item) for item in value]
    return value
