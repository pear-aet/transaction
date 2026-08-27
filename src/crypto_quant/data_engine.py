from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .data import CcxtDataSource, timeframe_ms, validate_candles
from .domain import Candle, FundingRate
from .market_stream import ClosedCandleEvent, MarkPriceEvent
from .storage import Repository


@dataclass(frozen=True, slots=True)
class SyncResult:
    symbol: str
    timeframe: str
    price_type: str
    fetched: int
    stored: int
    first_timestamp: int | None
    last_timestamp: int | None


@dataclass(slots=True)
class DataEngineHealth:
    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    last_sync_ms: int | None = None
    last_event_ms: int | None = None
    gaps_repaired: int = 0
    duplicate_candles: int = 0
    duplicate_funding_rates: int = 0
    rejected_events: int = 0
    last_error: str | None = None

    def snapshot(self, now_ms: int | None = None) -> dict[str, Any]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        return {
            "started_at_ms": self.started_at_ms,
            "uptime_ms": max(0, now - self.started_at_ms),
            "last_sync_ms": self.last_sync_ms,
            "last_event_ms": self.last_event_ms,
            "gaps_repaired": self.gaps_repaired,
            "duplicate_candles": self.duplicate_candles,
            "duplicate_funding_rates": self.duplicate_funding_rates,
            "rejected_events": self.rejected_events,
            "last_error": self.last_error,
        }


class MarketDataEngine:
    """Single immutable gate for historical and live market data."""

    def __init__(self, repository: Repository, source: CcxtDataSource | None = None) -> None:
        self.repository = repository
        self.source = source or CcxtDataSource()
        self.health = DataEngineHealth()
        self.latest_mark_prices: dict[str, MarkPriceEvent] = {}

    @staticmethod
    def _incremental_cursor(
        latest: int | None,
        since: int | None,
        step: int,
    ) -> int | None:
        if latest is None:
            return since
        next_value = latest + step
        return next_value if since is None else max(since, next_value)

    def sync_candles(
        self,
        symbol: str,
        timeframe: str,
        *,
        since: int | None = None,
        price_type: str = "contract",
        max_candles: int = 5000,
    ) -> SyncResult:
        if max_candles <= 0:
            raise ValueError("max_candles must be positive")
        interval = timeframe_ms(timeframe)
        latest = self.repository.latest_candle_timestamp(symbol, timeframe, price_type)
        cursor = self._incremental_cursor(latest, since, interval)
        fetcher = (
            self.source.fetch_mark_candles if price_type == "mark" else self.source.fetch_candles
        )
        try:
            candles = fetcher(symbol, timeframe, cursor, max_candles=max_candles)
            validate_candles(candles, interval)
            inserted, duplicates = self.repository.store_candles_immutable(
                symbol,
                timeframe,
                candles,
                price_type,
            )
        except Exception as exc:
            self.health.last_error = f"{exc.__class__.__name__}: {exc}"
            raise
        self.health.duplicate_candles += duplicates
        self.health.last_sync_ms = int(time.time() * 1000)
        self.health.last_error = None
        return SyncResult(
            symbol,
            timeframe,
            price_type,
            len(candles),
            inserted,
            None if not candles else candles[0].timestamp,
            None if not candles else candles[-1].timestamp,
        )

    def sync_funding(
        self, symbol: str, *, since: int | None = None, max_rates: int = 5000
    ) -> list[FundingRate]:
        if max_rates <= 0:
            raise ValueError("max_rates must be positive")
        latest = self.repository.latest_funding_timestamp(symbol)
        cursor = self._incremental_cursor(latest, since, 1)
        try:
            rates = self.source.fetch_funding_rates(symbol, cursor, max_rates=max_rates)
            timestamps = [item.timestamp for item in rates]
            if timestamps != sorted(set(timestamps)):
                raise ValueError("funding history contains duplicate or out-of-order timestamps")
            _, duplicates = self.repository.store_funding_immutable(symbol, rates)
        except Exception as exc:
            self.health.last_error = f"{exc.__class__.__name__}: {exc}"
            raise
        self.health.duplicate_funding_rates += duplicates
        self.health.last_sync_ms = int(time.time() * 1000)
        self.health.last_error = None
        return rates

    def repair_gap(
        self,
        symbol: str,
        timeframe: str,
        missing_start: int,
        missing_end: int,
        price_type: str = "contract",
    ) -> list[Candle]:
        interval = timeframe_ms(timeframe)
        if missing_start > missing_end:
            return []
        if missing_start % interval or missing_end % interval:
            raise ValueError("gap boundaries must be aligned to the candle timeframe")
        expected = ((missing_end - missing_start) // interval) + 1
        fetcher = (
            self.source.fetch_mark_candles if price_type == "mark" else self.source.fetch_candles
        )
        repaired = fetcher(
            symbol,
            timeframe,
            missing_start,
            max_candles=expected,
        )
        repaired = [item for item in repaired if missing_start <= item.timestamp <= missing_end]
        validate_candles(repaired, interval)
        expected_timestamps = list(range(missing_start, missing_end + interval, interval))
        if [item.timestamp for item in repaired] != expected_timestamps:
            raise RuntimeError(
                f"gap repair incomplete for {symbol} {timeframe}: "
                f"expected {expected} aligned candles, got {len(repaired)}"
            )
        inserted, duplicates = self.repository.store_candles_immutable(
            symbol,
            timeframe,
            repaired,
            price_type,
        )
        stored = self.repository.count_candles(
            symbol,
            timeframe,
            missing_start,
            missing_end,
            price_type,
        )
        if stored != expected or inserted + duplicates != expected:
            raise RuntimeError("gap repair did not persist the complete interval")
        self.health.duplicate_candles += duplicates
        self.health.gaps_repaired += 1
        self.health.last_sync_ms = int(time.time() * 1000)
        self.health.last_error = None
        return repaired

    def ingest_closed_candle(
        self,
        event: ClosedCandleEvent,
        *,
        canonical_symbol: str,
        price_type: str = "contract",
    ) -> Candle:
        interval = timeframe_ms(event.timeframe)
        candle = event.candle
        validate_candles([candle], interval)
        latest = self.repository.latest_candle_timestamp(
            canonical_symbol, event.timeframe, price_type
        )
        if latest is not None and candle.timestamp > latest + interval:
            self.repair_gap(
                canonical_symbol,
                event.timeframe,
                latest + interval,
                candle.timestamp - interval,
                price_type,
            )
        inserted, duplicates = self.repository.store_candles_immutable(
            canonical_symbol,
            event.timeframe,
            [candle],
            price_type,
        )
        if inserted + duplicates != 1:
            raise RuntimeError("closed candle persistence returned an invalid count")
        self.health.duplicate_candles += duplicates
        self.health.last_event_ms = event.event_time
        self.health.last_error = None
        return candle

    def ingest_event(
        self,
        event: ClosedCandleEvent | MarkPriceEvent,
        *,
        symbol_map: dict[str, str] | None = None,
    ) -> Candle | MarkPriceEvent:
        try:
            if isinstance(event, MarkPriceEvent):
                canonical = (symbol_map or {}).get(event.symbol, event.symbol)
                previous = self.latest_mark_prices.get(canonical)
                if previous is None or event.event_time >= previous.event_time:
                    self.latest_mark_prices[canonical] = event
                    self.health.last_event_ms = event.event_time
                return event
            canonical = (symbol_map or {}).get(event.symbol, event.symbol)
            return self.ingest_closed_candle(event, canonical_symbol=canonical)
        except Exception as exc:
            self.health.rejected_events += 1
            self.health.last_error = f"{exc.__class__.__name__}: {exc}"
            raise
