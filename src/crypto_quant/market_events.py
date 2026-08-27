from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from .data import timeframe_ms
from .domain import Candle, FundingRate
from .market_stream import ClosedCandleEvent, MarkPriceEvent


class MarketEventKind(StrEnum):
    CANDLE_CLOSED = "candle_closed"
    MARK_PRICE = "mark_price"
    FUNDING_RATE = "funding_rate"


_KIND_PRIORITY = {
    MarketEventKind.MARK_PRICE: 0,
    MarketEventKind.FUNDING_RATE: 1,
    MarketEventKind.CANDLE_CLOSED: 2,
}


@dataclass(frozen=True, slots=True)
class CandleClosedPayload:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.open_time < 0 or self.close_time < self.open_time:
            raise ValueError("invalid candle payload timestamps")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("candle payload contains a non-finite value")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("candle payload prices must be positive")
        if self.volume < 0:
            raise ValueError("candle payload volume must be non-negative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("invalid candle payload OHLC relationship")

    @classmethod
    def from_candle(cls, candle: Candle, timeframe: str) -> CandleClosedPayload:
        interval = timeframe_ms(timeframe)
        return cls(
            candle.timestamp,
            candle.timestamp + interval - 1,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
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


@dataclass(frozen=True, slots=True)
class FundingRatePayload:
    rate: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.rate):
            raise ValueError("funding rate must be finite")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


MarketPayload = CandleClosedPayload | MarkPricePayload | FundingRatePayload


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
    price_type: str | None = None
    sequence: int = 0
    storage_sequence: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.event_id or not self.symbol:
            raise ValueError("event_id and symbol must not be empty")
        timestamps = (self.event_time, self.source_time, self.received_at, self.sequence)
        if min(timestamps) < 0:
            raise ValueError("event timestamps and sequence must be non-negative")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("storage_sequence must be positive")
        if self.schema_version <= 0:
            raise ValueError("schema_version must be positive")
        if self.kind is MarketEventKind.CANDLE_CLOSED:
            if not self.timeframe or not self.price_type:
                raise ValueError("closed-candle events require timeframe and price_type")
            if not isinstance(self.payload, CandleClosedPayload):
                raise ValueError("closed-candle events require a candle payload")
            interval = timeframe_ms(self.timeframe)
            if self.event_time != self.payload.open_time + interval:
                raise ValueError("candle event_time must be the exclusive close boundary")
            if self.payload.close_time != self.event_time - 1:
                raise ValueError("candle close_time does not match timeframe")
        elif self.kind is MarketEventKind.MARK_PRICE:
            if self.timeframe is not None or self.price_type != "mark":
                raise ValueError("mark-price events require price_type=mark and no timeframe")
            if not isinstance(self.payload, MarkPricePayload):
                raise ValueError("mark-price events require a mark-price payload")
        elif self.kind is MarketEventKind.FUNDING_RATE:
            if self.timeframe is not None or self.price_type is not None:
                raise ValueError("funding events have no timeframe or price_type")
            if not isinstance(self.payload, FundingRatePayload):
                raise ValueError("funding events require a funding payload")
        else:
            raise ValueError(f"unsupported market event kind: {self.kind}")

    @property
    def durable(self) -> bool:
        return self.kind is not MarketEventKind.MARK_PRICE

    @property
    def latency_ms(self) -> int:
        return max(0, self.received_at - self.event_time)

    @property
    def partition_key(self) -> str:
        if self.kind is MarketEventKind.CANDLE_CLOSED:
            return f"{self.symbol}:candle:{self.price_type}:{self.timeframe}"
        return f"{self.symbol}:{self.kind.value}"

    @property
    def sort_key(self) -> tuple[int, int, int, str, str]:
        timeframe_priority = (
            -timeframe_ms(self.timeframe)
            if self.kind is MarketEventKind.CANDLE_CLOSED and self.timeframe
            else 0
        )
        return (
            self.event_time,
            _KIND_PRIORITY[self.kind],
            timeframe_priority,
            self.symbol,
            self.event_id,
        )

    def with_sequence(self, sequence: int) -> MarketEvent:
        if sequence <= 0:
            raise ValueError("published event sequence must be positive")
        return replace(self, sequence=sequence)

    def with_storage_sequence(self, sequence: int) -> MarketEvent:
        if sequence <= 0:
            raise ValueError("storage sequence must be positive")
        return replace(self, storage_sequence=sequence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "price_type": self.price_type,
            "event_time": self.event_time,
            "source_time": self.source_time,
            "received_at": self.received_at,
            "sequence": self.sequence,
            "storage_sequence": self.storage_sequence,
            "schema_version": self.schema_version,
            "latency_ms": self.latency_ms,
            "payload": self.payload.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MarketEvent:
        kind = MarketEventKind(value["kind"])
        payload_value = dict(value["payload"])
        if kind is MarketEventKind.CANDLE_CLOSED:
            payload: MarketPayload = CandleClosedPayload(**payload_value)
        elif kind is MarketEventKind.MARK_PRICE:
            payload = MarkPricePayload(**payload_value)
        else:
            payload = FundingRatePayload(**payload_value)
        return cls(
            event_id=str(value["event_id"]),
            kind=kind,
            symbol=str(value["symbol"]),
            event_time=int(value["event_time"]),
            source_time=int(value["source_time"]),
            received_at=int(value["received_at"]),
            payload=payload,
            timeframe=value.get("timeframe"),
            price_type=value.get("price_type"),
            sequence=int(value.get("sequence", 0)),
            storage_sequence=(
                None
                if value.get("storage_sequence") is None
                else int(value["storage_sequence"])
            ),
            schema_version=int(value.get("schema_version", 1)),
        )


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
        return self.price_types is None or event.price_type in self.price_types

    def to_dict(self) -> dict[str, list[str] | None]:
        return {
            "kinds": None if self.kinds is None else sorted(item.value for item in self.kinds),
            "symbols": None if self.symbols is None else sorted(self.symbols),
            "timeframes": None if self.timeframes is None else sorted(self.timeframes),
            "price_types": None if self.price_types is None else sorted(self.price_types),
        }


def _stable_event_id(
    kind: MarketEventKind,
    symbol: str,
    event_time: int,
    timeframe: str | None,
    price_type: str | None,
    identity: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "kind": kind.value,
            "symbol": symbol,
            "event_time": event_time,
            "timeframe": timeframe,
            "price_type": price_type,
            "identity": identity,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.blake2s(canonical.encode("utf-8"), digest_size=16).hexdigest()
    return f"{kind.value}:{digest}"


def candle_market_event(
    symbol: str,
    timeframe: str,
    candle: Candle,
    *,
    source_time: int | None = None,
    price_type: str = "contract",
    received_at: int | None = None,
) -> MarketEvent:
    payload = CandleClosedPayload.from_candle(candle, timeframe)
    event_time = candle.timestamp + timeframe_ms(timeframe)
    identity = {
        "open_time": payload.open_time,
        "open": format(payload.open, ".17g"),
        "high": format(payload.high, ".17g"),
        "low": format(payload.low, ".17g"),
        "close": format(payload.close, ".17g"),
        "volume": format(payload.volume, ".17g"),
    }
    return MarketEvent(
        event_id=_stable_event_id(
            MarketEventKind.CANDLE_CLOSED,
            symbol,
            event_time,
            timeframe,
            price_type,
            identity,
        ),
        kind=MarketEventKind.CANDLE_CLOSED,
        symbol=symbol,
        event_time=event_time,
        source_time=event_time if source_time is None else source_time,
        received_at=int(time.time() * 1000) if received_at is None else received_at,
        payload=payload,
        timeframe=timeframe,
        price_type=price_type,
    )


def closed_candle_market_event(
    event: ClosedCandleEvent,
    canonical_symbol: str,
    *,
    price_type: str = "contract",
    received_at: int | None = None,
) -> MarketEvent:
    return candle_market_event(
        canonical_symbol,
        event.timeframe,
        event.candle,
        source_time=event.event_time,
        price_type=price_type,
        received_at=received_at,
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
    identity = {
        "mark_price": format(payload.mark_price, ".17g"),
        "index_price": format(payload.index_price, ".17g"),
        "funding_rate": (
            None if payload.funding_rate is None else format(payload.funding_rate, ".17g")
        ),
        "next_funding_time": payload.next_funding_time,
    }
    return MarketEvent(
        event_id=_stable_event_id(
            MarketEventKind.MARK_PRICE,
            canonical_symbol,
            event.event_time,
            None,
            "mark",
            identity,
        ),
        kind=MarketEventKind.MARK_PRICE,
        symbol=canonical_symbol,
        event_time=event.event_time,
        source_time=event.event_time,
        received_at=int(time.time() * 1000) if received_at is None else received_at,
        payload=payload,
        price_type="mark",
    )


def funding_rate_market_event(
    symbol: str,
    funding: FundingRate,
    *,
    received_at: int | None = None,
) -> MarketEvent:
    payload = FundingRatePayload(funding.rate)
    identity = {"rate": format(payload.rate, ".17g")}
    return MarketEvent(
        event_id=_stable_event_id(
            MarketEventKind.FUNDING_RATE,
            symbol,
            funding.timestamp,
            None,
            None,
            identity,
        ),
        kind=MarketEventKind.FUNDING_RATE,
        symbol=symbol,
        event_time=funding.timestamp,
        source_time=funding.timestamp,
        received_at=int(time.time() * 1000) if received_at is None else received_at,
        payload=payload,
    )


def candle_from_market_event(event: MarketEvent) -> Candle:
    if event.kind is not MarketEventKind.CANDLE_CLOSED:
        raise ValueError("event is not a closed candle")
    payload = event.payload
    assert isinstance(payload, CandleClosedPayload)
    return Candle(
        payload.open_time,
        payload.open,
        payload.high,
        payload.low,
        payload.close,
        payload.volume,
    )


def funding_from_market_event(event: MarketEvent) -> FundingRate:
    if event.kind is not MarketEventKind.FUNDING_RATE:
        raise ValueError("event is not a funding rate")
    payload = event.payload
    assert isinstance(payload, FundingRatePayload)
    return FundingRate(event.event_time, payload.rate)
