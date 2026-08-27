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
            "rejected_events": self.rejected_events,
            "last_error": self.last_error,
        }


class MarketDataEngine:
    """Single gate for historical and live market data consumed by trading logic."""

    def __init__(self, repository: Repository, source: CcxtDataSource | None = None) -> None:
        self.repository = repository
        self.source = source or CcxtDataSource()
        self.health = DataEngineHealth()
        self.latest_mark_prices: dict[str, MarkPriceEvent] = {}

    def _latest_timestamp(self, symbol: str, timeframe: str, price_type: str) -> int | None:
        with self.repository._connect() as connection:
            row = connection.execute(
                """SELECT max(timestamp) FROM candles
                WHERE symbol=? AND timeframe=? AND price_type=?""",
                (symbol, timeframe, price_type),
            ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def _count_range(
        self, symbol: str, timeframe: str, price_type: str, start: int, end: int
    ) -> int:
        with self.repository._connect() as connection:
            row = connection.execute(
                """SELECT count(*) FROM candles
                WHERE symbol=? AND timeframe=? AND price_type=?
                AND timestamp>=? AND timestamp<=?""",
                (symbol, timeframe, price_type, start, end),
            ).fetchone()
        return int(row[0])

    def sync_candles(
        self,
        symbol: str,
        timeframe: str,
        *,
        since: int | None = None,
        price_type: str = "contract",
        max_candles: int = 5000,
    ) -> SyncResult:
        interval = timeframe_ms(timeframe)
        latest = self._latest_timestamp(symbol, timeframe, price_type)
        cursor = since if since is not None else (None if latest is None else latest + interval)
        fetcher = (
            self.source.fetch_mark_candles if price_type == "mark" else self.source.fetch_candles
        )
        candles = fetcher(symbol, timeframe, cursor, max_candles=max_candles)
        validate_candles(candles, interval)
        if candles:
            self.repository.upsert_candles(symbol, timeframe, candles, price_type)
        self.health.last_sync_ms = int(time.time() * 1000)
        self.health.last_error = None
        return SyncResult(
            symbol,
            timeframe,
            price_type,
            len(candles),
            len(candles),
            None if not candles else candles[0].timestamp,
            None if not candles else candles[-1].timestamp,
        )

    def sync_funding(
        self, symbol: str, *, since: int | None = None, max_rates: int = 5000
    ) -> list[FundingRate]:
        if since is None:
            with self.repository._connect() as connection:
                row = connection.execute(
                    "SELECT max(timestamp) FROM funding_rates WHERE symbol=?", (symbol,)
                ).fetchone()
            since = None if row is None or row[0] is None else int(row[0]) + 1
        rates = self.source.fetch_funding_rates(symbol, since, max_rates=max_rates)
        if rates:
            self.repository.upsert_funding(symbol, rates)
        self.health.last_sync_ms = int(time.time() * 1000)
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
        if len(repaired) != expected:
            raise RuntimeError(
                f"gap repair incomplete for {symbol} {timeframe}: "
                f"expected {expected}, got {len(repaired)}"
            )
        self.repository.upsert_candles(symbol, timeframe, repaired, price_type)
        if self._count_range(symbol, timeframe, price_type, missing_start, missing_end) != expected:
            raise RuntimeError("gap repair did not persist the complete interval")
        self.health.gaps_repaired += 1
        self.health.last_sync_ms = int(time.time() * 1000)
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
        latest = self._latest_timestamp(canonical_symbol, event.timeframe, price_type)
        if latest is not None:
            if candle.timestamp <= latest:
                self.repository.upsert_candles(
                    canonical_symbol, event.timeframe, [candle], price_type
                )
                self.health.last_event_ms = event.event_time
                return candle
            expected_next = latest + interval
            if candle.timestamp > expected_next:
                self.repair_gap(
                    canonical_symbol,
                    event.timeframe,
                    expected_next,
                    candle.timestamp - interval,
                    price_type,
                )
        self.repository.upsert_candles(canonical_symbol, event.timeframe, [candle], price_type)
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
                self.latest_mark_prices[canonical] = event
                self.health.last_event_ms = event.event_time
                return event
            canonical = (symbol_map or {}).get(event.symbol, event.symbol)
            return self.ingest_closed_candle(event, canonical_symbol=canonical)
        except Exception as exc:
            self.health.rejected_events += 1
            self.health.last_error = f"{exc.__class__.__name__}: {exc}"
            raise
