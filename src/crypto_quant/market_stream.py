from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from .data import timeframe_ms
from .domain import Candle


@dataclass(frozen=True, slots=True)
class ClosedCandleEvent:
    symbol: str
    timeframe: str
    candle: Candle
    event_time: int


@dataclass(frozen=True, slots=True)
class MarkPriceEvent:
    symbol: str
    mark_price: float
    index_price: float
    funding_rate: float | None
    next_funding_time: int | None
    event_time: int


@dataclass(slots=True)
class StreamHealth:
    connected: bool = False
    reconnects: int = 0
    last_message_ms: int | None = None
    last_closed_candle_ms: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None

    def snapshot(self, now_ms: int | None = None) -> dict[str, Any]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        age = None if self.last_message_ms is None else max(0, now - self.last_message_ms)
        return {
            "connected": self.connected,
            "reconnects": self.reconnects,
            "last_message_ms": self.last_message_ms,
            "message_age_ms": age,
            "last_closed_candle_ms": dict(self.last_closed_candle_ms),
            "last_error": self.last_error,
        }


def binance_stream_symbol(symbol: str) -> str:
    base = symbol.split(":", 1)[0].replace("/", "")
    if not base:
        raise ValueError("symbol must not be empty")
    return base.lower()


def parse_binance_stream_message(
    payload: str | bytes | dict[str, Any],
) -> ClosedCandleEvent | MarkPriceEvent | None:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    message = json.loads(payload) if isinstance(payload, str) else payload
    data = message.get("data", message)
    event_type = data.get("e")

    if event_type == "kline":
        kline = data.get("k") or {}
        if not kline.get("x"):
            return None
        symbol = str(data.get("s") or kline.get("s") or "")
        timeframe = str(kline.get("i") or "")
        candle = Candle(
            int(kline["t"]),
            float(kline["o"]),
            float(kline["h"]),
            float(kline["l"]),
            float(kline["c"]),
            float(kline["v"]),
        )
        if int(kline["T"]) + 1 - candle.timestamp != timeframe_ms(timeframe):
            raise ValueError("stream candle interval does not match timeframe")
        return ClosedCandleEvent(symbol, timeframe, candle, int(data.get("E") or kline["T"]))

    if event_type == "markPriceUpdate":
        funding = data.get("r")
        next_funding = data.get("T")
        return MarkPriceEvent(
            str(data["s"]),
            float(data["p"]),
            float(data["i"]),
            None if funding in (None, "") else float(funding),
            None if next_funding in (None, "") else int(next_funding),
            int(data["E"]),
        )
    return None


class BinanceFuturesStream:
    """Public USD-M futures websocket stream with reconnect and closed-bar gating."""

    base_url = "wss://fstream.binance.com/stream?streams="

    def __init__(
        self,
        symbols: tuple[str, ...] | list[str],
        timeframes: tuple[str, ...] | list[str],
        include_mark_price: bool = True,
        max_backoff_seconds: float = 30.0,
    ) -> None:
        if not symbols or not timeframes:
            raise ValueError("symbols and timeframes must not be empty")
        if max_backoff_seconds <= 0:
            raise ValueError("max_backoff_seconds must be positive")
        for timeframe in timeframes:
            timeframe_ms(timeframe)
        self.symbols = tuple(symbols)
        self.timeframes = tuple(timeframes)
        self.include_mark_price = include_mark_price
        self.max_backoff_seconds = max_backoff_seconds
        self.health = StreamHealth()
        self._stop = asyncio.Event()

    def stream_names(self) -> list[str]:
        names = [
            f"{binance_stream_symbol(symbol)}@kline_{timeframe}"
            for symbol in self.symbols
            for timeframe in self.timeframes
        ]
        if self.include_mark_price:
            names.extend(f"{binance_stream_symbol(symbol)}@markPrice@1s" for symbol in self.symbols)
        return names

    @property
    def url(self) -> str:
        return self.base_url + "/".join(self.stream_names())

    def stop(self) -> None:
        self._stop.set()

    async def _backoff(self, attempt: int) -> int:
        delay = min(self.max_backoff_seconds, 0.5 * 2**attempt)
        delay += random.random() * min(0.5, delay * 0.1)
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            pass
        return min(attempt + 1, 10)

    async def events(self) -> AsyncIterator[ClosedCandleEvent | MarkPriceEvent]:
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "websockets dependency is required for real-time market data"
            ) from exc

        attempt = 0
        while not self._stop.is_set():
            clean_disconnect = False
            try:
                async with connect(
                    self.url,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=1024,
                ) as websocket:
                    self.health.connected = True
                    self.health.last_error = None
                    attempt = 0
                    async for raw in websocket:
                        if self._stop.is_set():
                            self.health.connected = False
                            return
                        self.health.last_message_ms = int(time.time() * 1000)
                        event = parse_binance_stream_message(raw)
                        if event is None:
                            continue
                        if isinstance(event, ClosedCandleEvent):
                            key = f"{event.symbol}:{event.timeframe}"
                            previous = self.health.last_closed_candle_ms.get(key)
                            if previous is not None and event.candle.timestamp <= previous:
                                continue
                            self.health.last_closed_candle_ms[key] = event.candle.timestamp
                        yield event
                    clean_disconnect = True
            except asyncio.CancelledError:
                self.health.connected = False
                raise
            except Exception as exc:
                self.health.connected = False
                self.health.last_error = f"{exc.__class__.__name__}: {exc}"
                self.health.reconnects += 1
                attempt = await self._backoff(attempt)
                continue

            self.health.connected = False
            if clean_disconnect and not self._stop.is_set():
                self.health.reconnects += 1
                self.health.last_error = "stream ended without a close exception"
                attempt = await self._backoff(attempt)

        self.health.connected = False

    async def run(self, handler: Callable[[ClosedCandleEvent | MarkPriceEvent], Any]) -> None:
        async for event in self.events():
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result
