from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .data_engine import MarketDataEngine, SyncResult
from .market_bus import MarketEventBus, PublishResult
from .market_events import closed_candle_market_event, mark_price_market_event
from .market_stream import (
    BinanceFuturesStream,
    ClosedCandleEvent,
    MarkPriceEvent,
    binance_stream_symbol,
)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    contract_candles: tuple[SyncResult, ...]
    mark_candles: tuple[SyncResult, ...]
    funding_rows: dict[str, int]


@dataclass(slots=True)
class MarketRuntimeHealth:
    running: bool = False
    started_at_ms: int | None = None
    stopped_at_ms: int | None = None
    received: int = 0
    accepted: int = 0
    published: int = 0
    duplicates: int = 0
    failures: int = 0
    blocking_calls_completed: int = 0
    last_event_ms: int | None = None
    last_sequence: int = 0
    last_event_id: str | None = None
    last_error: str | None = None
    bootstrap_completed_at_ms: int | None = None
    bootstrap_runs: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at_ms": self.started_at_ms,
            "stopped_at_ms": self.stopped_at_ms,
            "received": self.received,
            "accepted": self.accepted,
            "published": self.published,
            "duplicates": self.duplicates,
            "failures": self.failures,
            "blocking_calls_completed": self.blocking_calls_completed,
            "last_event_ms": self.last_event_ms,
            "last_sequence": self.last_sequence,
            "last_event_id": self.last_event_id,
            "last_error": self.last_error,
            "bootstrap_completed_at_ms": self.bootstrap_completed_at_ms,
            "bootstrap_runs": self.bootstrap_runs,
        }


def build_binance_symbol_map(symbols: tuple[str, ...] | list[str]) -> dict[str, str]:
    if not symbols:
        raise ValueError("at least one canonical symbol is required")
    output: dict[str, str] = {}
    for canonical in symbols:
        raw = binance_stream_symbol(canonical).upper()
        existing = output.get(raw)
        if existing is not None and existing != canonical:
            raise ValueError(f"canonical symbols collide on Binance stream symbol: {raw}")
        output[raw] = canonical
    return output


class MarketRuntime:
    """Fail-closed bridge from Binance market data to the canonical event bus."""

    def __init__(
        self,
        stream: BinanceFuturesStream,
        data_engine: MarketDataEngine,
        bus: MarketEventBus,
        *,
        symbol_map: dict[str, str] | None = None,
        strict_symbols: bool = True,
    ) -> None:
        self.stream = stream
        self.data_engine = data_engine
        self.bus = bus
        if symbol_map is None:
            derived = build_binance_symbol_map(list(stream.symbols))
        else:
            derived = symbol_map
        normalized: dict[str, str] = {}
        for raw_symbol, canonical in derived.items():
            key = raw_symbol.upper()
            existing = normalized.get(key)
            if existing is not None and existing != canonical:
                raise ValueError(f"conflicting symbol mapping for {key}")
            normalized[key] = canonical
        if not normalized or any(not key or not value for key, value in normalized.items()):
            raise ValueError("symbol_map must contain non-empty symbols")
        missing = set(stream.symbols) - set(normalized.values())
        if missing:
            missing_text = ", ".join(sorted(missing))
            raise ValueError(f"symbol_map does not cover configured symbols: {missing_text}")
        self.symbol_map = normalized
        self.strict_symbols = strict_symbols
        self.health = MarketRuntimeHealth()

    def canonical_symbol(self, raw_symbol: str) -> str:
        canonical = self.symbol_map.get(raw_symbol.upper())
        if canonical is None and self.strict_symbols:
            raise KeyError(f"unmapped market stream symbol: {raw_symbol}")
        return raw_symbol if canonical is None else canonical

    async def _blocking_call(self, function, /, *args, **kwargs):
        result = await asyncio.to_thread(function, *args, **kwargs)
        self.health.blocking_calls_completed += 1
        return result

    async def bootstrap(
        self,
        *,
        since: int | None = None,
        max_candles: int = 5000,
        include_mark_candles: bool = True,
        include_funding: bool = True,
    ) -> BootstrapResult:
        if since is not None and since < 0:
            raise ValueError("since must be non-negative")
        if max_candles <= 0:
            raise ValueError("max_candles must be positive")
        contract_results: list[SyncResult] = []
        mark_results: list[SyncResult] = []
        funding_rows: dict[str, int] = {}
        try:
            for symbol in self.stream.symbols:
                for timeframe in self.stream.timeframes:
                    contract_results.append(
                        await self._blocking_call(
                            self.data_engine.sync_candles,
                            symbol,
                            timeframe,
                            since=since,
                            price_type="contract",
                            max_candles=max_candles,
                        )
                    )
                    if include_mark_candles:
                        mark_results.append(
                            await self._blocking_call(
                                self.data_engine.sync_candles,
                                symbol,
                                timeframe,
                                since=since,
                                price_type="mark",
                                max_candles=max_candles,
                            )
                        )
                if include_funding:
                    rates = await self._blocking_call(
                        self.data_engine.sync_funding,
                        symbol,
                        since=since,
                        max_rates=max_candles,
                    )
                    funding_rows[symbol] = len(rates)
            self.health.bootstrap_runs += 1
            self.health.bootstrap_completed_at_ms = int(time.time() * 1000)
            self.health.last_error = None
            return BootstrapResult(
                tuple(contract_results),
                tuple(mark_results),
                funding_rows,
            )
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def handle_stream_event(
        self,
        event: ClosedCandleEvent | MarkPriceEvent,
    ) -> PublishResult:
        self.health.received += 1
        received_at = int(time.time() * 1000)
        try:
            canonical = self.canonical_symbol(event.symbol)
            await self._blocking_call(
                self.data_engine.ingest_event,
                event,
                symbol_map=self.symbol_map,
            )
            self.health.accepted += 1
            if isinstance(event, ClosedCandleEvent):
                market_event = closed_candle_market_event(
                    event,
                    canonical,
                    received_at=received_at,
                )
            else:
                market_event = mark_price_market_event(
                    event,
                    canonical,
                    received_at=received_at,
                )
            result = await self.bus.publish(market_event)
            self.health.last_event_ms = event.event_time
            self.health.last_error = None
            if result.duplicate:
                self.health.duplicates += 1
            elif result.event is not None:
                self.health.published += 1
                self.health.last_sequence = result.event.sequence
                self.health.last_event_id = result.event.event_id
            return result
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _record_failure(self, exc: Exception) -> None:
        message = f"{exc.__class__.__name__}: {exc}"
        if self.health.last_error != message:
            self.health.failures += 1
        self.health.last_error = message

    async def run(self) -> None:
        if self.health.running:
            raise RuntimeError("market runtime is already running")
        if self.bus.closed:
            raise RuntimeError("cannot run with a closed market event bus")
        self.health.running = True
        self.health.started_at_ms = int(time.time() * 1000)
        self.health.stopped_at_ms = None
        self.health.last_error = None
        try:
            await self.stream.run(self.handle_stream_event)
        except Exception as exc:
            self._record_failure(exc)
            raise
        finally:
            self.health.running = False
            self.health.stopped_at_ms = int(time.time() * 1000)

    def stop(self) -> None:
        self.stream.stop()

    async def close(self) -> None:
        self.stop()
        await self.bus.close()

    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "runtime": self.health.snapshot(),
            "stream": self.stream.health.snapshot(),
            "data_engine": self.data_engine.health.snapshot(),
            "event_bus": await self.bus.health_snapshot(),
        }
