from __future__ import annotations

import hashlib
import math
import time
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from .domain import Candle
from .market_stream import ClosedCandleEvent, MarkPriceEvent


class MarketEventKind(StrEnum):
    CANDLE_CLOSED = "candle_closed"
    MARK_PRICE = "mark_price"


@dataclass(frozen=True, slots=True)
class CandleClosedPayload:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    price_type: str = "contract"

    def __post_init__(self) -> None:
        if self.timestamp < 0:
            raise ValueError("candle timestamp must be non-negative")
        if not self.price_type:
            raise ValueError("price_type must not be empty")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("candle payload contains a non-finite value")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("candle payload prices must be positive")
        if self.volume < 0:
            raise ValueError("candle payload volume must be non-negative")
        if self.low > min(self.open, self.close):
            raise ValueError("candle payload low exceeds the candle body")
        if self.high < max(self.open, self.close):
            raise ValueError("candle payload high is below the candle body")

    @classmethod
    def from_candle(
        cls,
        candle: Candle,
        price_type: str = "contract",
    ) -> CandleClosedPayload:
        return cls(
            candle.timestamp,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            price_type,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MarkPricePayload:
    mark_price: float
    index_price: float
    funding_rate: float | None
    next_funding_time: int | None

    def __post_init__(self) -> None:
        if not math.isfinite(self.mark_price) or self.mark_price <= 0:
            raise ValueError("mark_price must be finite and positive")
        if not math.isfinite(self.index_price) or self.index_price <= 0:
            raise ValueError("index_price must be finite and positive")
        if self.funding_rate is not None and not math.isfinite(self.funding_rate):
            raise ValueError("funding_rate must be finite")
        if self.next_funding_time is not None and self.next_funding_time < 0:
            raise ValueError("next_funding_time must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


MarketPayload = CandleClosedPayload | MarkPricePayload


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_id: str
    kind: MarketEventKind
    symbol: str
    event_time: int
    source_time: int
    received_at: int
    payload: MarketPayload
    timeframe: str | None = None
    sequence: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id must not be empty")
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if min(self.event_time, self.source_time, self.received_at, self.sequence) < 0:
            raise ValueError("event timestamps and sequence must be non-negative")
        if self.schema_version <= 0:
            raise ValueError("schema_version must be positive")
        if self.kind is MarketEventKind.CANDLE_CLOSED:
            if not self.timeframe or not isinstance(self.payload, CandleClosedPayload):
                raise ValueError("closed-candle events require a timeframe and candle payload")
        elif self.kind is MarketEventKind.MARK_PRICE:
            if self.timeframe is not None or not isinstance(self.payload, MarkPricePayload):
                raise ValueError("mark-price events require a mark-price payload and no timeframe")
        else:
            raise ValueError(f"unsupported market event kind: {self.kind}")

    @property
    def latency_ms(self) -> int:
        return max(0, self.received_at - self.event_time)

    def with_sequence(self, sequence: int) -> MarketEvent:
        if sequence <= 0:
            raise ValueError("published event sequence must be positive")
        return replace(self, sequence=sequence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "event_time": self.event_time,
            "source_time": self.source_time,
            "received_at": self.received_at,
            "sequence": self.sequence,
            "schema_version": self.schema_version,
            "latency_ms": self.latency_ms,
            "payload": self.payload.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class MarketEventFilter:
    kinds: frozenset[MarketEventKind] | None = None
    symbols: frozenset[str] | None = None
    timeframes: frozenset[str] | None = None
    price_types: frozenset[str] | None = None

    def matches(self, event: MarketEvent) -> bool:
        if self.kinds is not None and event.kind not in self.kinds:
            return False
        if self.symbols is not None and event.symbol not in self.symbols:
            return False
        if self.timeframes is not None and event.timeframe not in self.timeframes:
            return False
        price_type = None
        if isinstance(event.payload, CandleClosedPayload):
            price_type = event.payload.price_type
        return self.price_types is None or price_type in self.price_types


def _stable_event_id(kind: MarketEventKind, *parts: object) -> str:
    canonical = "\x1f".join(str(part) for part in (kind.value, *parts))
    digest = hashlib.blake2s(canonical.encode("utf-8"), digest_size=16).hexdigest()
    return f"{kind.value}:{digest}"


def closed_candle_market_event(
    event: ClosedCandleEvent,
    canonical_symbol: str,
    *,
    price_type: str = "contract",
    received_at: int | None = None,
) -> MarketEvent:
    payload = CandleClosedPayload.from_candle(event.candle, price_type)
    identifier = _stable_event_id(
        MarketEventKind.CANDLE_CLOSED,
        canonical_symbol,
        event.timeframe,
        price_type,
        payload.timestamp,
        format(payload.open, ".17g"),
        format(payload.high, ".17g"),
        format(payload.low, ".17g"),
        format(payload.close, ".17g"),
        format(payload.volume, ".17g"),
    )
    return MarketEvent(
        identifier,
        MarketEventKind.CANDLE_CLOSED,
        canonical_symbol,
        event.event_time,
        event.candle.timestamp,
        int(time.time() * 1000) if received_at is None else received_at,
        payload,
        event.timeframe,
    )


def mark_price_market_event(
    event: MarkPriceEvent,
    canonical_symbol: str,
    *,
    received_at: int | None = None,
) -> MarketEvent:
    payload = MarkPricePayload(
        event.mark_price,
        event.index_price,
        event.funding_rate,
        event.next_funding_time,
    )
    identifier = _stable_event_id(
        MarketEventKind.MARK_PRICE,
        canonical_symbol,
        event.event_time,
        format(payload.mark_price, ".17g"),
        format(payload.index_price, ".17g"),
        "" if payload.funding_rate is None else format(payload.funding_rate, ".17g"),
        payload.next_funding_time,
    )
    return MarketEvent(
        identifier,
        MarketEventKind.MARK_PRICE,
        canonical_symbol,
        event.event_time,
        event.event_time,
        int(time.time() * 1000) if received_at is None else received_at,
        payload,
    )
