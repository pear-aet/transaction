import asyncio
import threading

import pytest

from crypto_quant.data_engine import SyncResult
from crypto_quant.domain import Candle, FundingRate
from crypto_quant.market_bus import MarketEventBus
from crypto_quant.market_runtime import MarketRuntime, build_binance_symbol_map
from crypto_quant.market_stream import ClosedCandleEvent, MarkPriceEvent


class Snapshot:
    def snapshot(self) -> dict[str, bool]:
        return {"ok": True}


class FakeStream:
    symbols = ("BTC/USDT:USDT",)
    timeframes = ("1h", "15m")

    def __init__(self, events=()) -> None:
        self.events = tuple(events)
        self.health = Snapshot()
        self.stopped = False

    async def run(self, handler) -> None:
        for event in self.events:
            await handler(event)

    def stop(self) -> None:
        self.stopped = True


class FailingStream(FakeStream):
    async def run(self, handler) -> None:
        raise RuntimeError("stream failed")


class FakeDataEngine:
    def __init__(self) -> None:
        self.health = Snapshot()
        self.ingested = []
        self.ingest_threads: list[int] = []
        self.sync_calls = []
        self.sync_threads: list[int] = []
        self.fail_ingest = False

    def ingest_event(self, event, *, symbol_map=None):
        self.ingest_threads.append(threading.get_ident())
        if self.fail_ingest:
            raise RuntimeError("storage unavailable")
        canonical = (symbol_map or {}).get(event.symbol, event.symbol)
        self.ingested.append((canonical, event))
        return event

    def sync_candles(
        self,
        symbol,
        timeframe,
        *,
        since=None,
        price_type="contract",
        max_candles=5000,
    ):
        self.sync_threads.append(threading.get_ident())
        self.sync_calls.append((symbol, timeframe, since, price_type, max_candles))
        return SyncResult(symbol, timeframe, price_type, 1, 1, 0, 0)

    def sync_funding(self, symbol, *, since=None, max_rates=5000):
        self.sync_threads.append(threading.get_ident())
        self.sync_calls.append((symbol, "funding", since, "funding", max_rates))
        return [FundingRate(1, 0.0001), FundingRate(2, 0.0002)]


def closed_event(symbol: str = "BTCUSDT") -> ClosedCandleEvent:
    return ClosedCandleEvent(
        symbol,
        "15m",
        Candle(0, 100.0, 102.0, 99.0, 101.0, 5.0),
        899_999,
    )


def test_runtime_persists_then_publishes_and_deduplicates() -> None:
    async def scenario() -> None:
        main_thread = threading.get_ident()
        stream = FakeStream()
        data_engine = FakeDataEngine()
        bus = MarketEventBus()
        subscription = await bus.subscribe("strategy")
        runtime = MarketRuntime(stream, data_engine, bus)

        first = await runtime.handle_stream_event(closed_event())
        duplicate = await runtime.handle_stream_event(closed_event())
        consumed = await subscription.get(timeout=1.0)

        assert first.event is not None and first.event.sequence == 1
        assert duplicate.duplicate is True
        assert consumed.symbol == "BTC/USDT:USDT"
        assert consumed.timeframe == "15m"
        assert [item[0] for item in data_engine.ingested] == [
            "BTC/USDT:USDT",
            "BTC/USDT:USDT",
        ]
        assert all(identifier != main_thread for identifier in data_engine.ingest_threads)
        assert runtime.health.received == 2
        assert runtime.health.accepted == 2
        assert runtime.health.published == 1
        assert runtime.health.duplicates == 1
        assert runtime.health.blocking_calls_completed == 2
        await runtime.close()
        assert stream.stopped is True

    asyncio.run(scenario())


def test_runtime_fails_closed_on_storage_or_symbol_errors() -> None:
    async def scenario() -> None:
        main_thread = threading.get_ident()
        data_engine = FakeDataEngine()
        data_engine.fail_ingest = True
        bus = MarketEventBus()
        subscription = await bus.subscribe("strategy")
        runtime = MarketRuntime(FakeStream(), data_engine, bus)

        with pytest.raises(RuntimeError, match="storage unavailable"):
            await runtime.handle_stream_event(closed_event())
        with pytest.raises(asyncio.TimeoutError):
            await subscription.get(timeout=0.01)
        assert data_engine.ingest_threads[0] != main_thread
        assert runtime.health.failures == 1
        assert runtime.health.accepted == 0

        data_engine.fail_ingest = False
        with pytest.raises(KeyError, match="unmapped"):
            await runtime.handle_stream_event(closed_event("ETHUSDT"))
        with pytest.raises(asyncio.TimeoutError):
            await subscription.get(timeout=0.01)
        assert runtime.health.failures == 2
        assert len(data_engine.ingest_threads) == 1
        await runtime.close()

    asyncio.run(scenario())


def test_runtime_bootstrap_and_health_cover_configured_data() -> None:
    async def scenario() -> None:
        main_thread = threading.get_ident()
        mark_update = MarkPriceEvent(
            "BTCUSDT",
            101.0,
            100.9,
            0.0001,
            3_600_000,
            1_000,
        )
        stream = FakeStream([mark_update])
        data_engine = FakeDataEngine()
        bus = MarketEventBus()
        subscription = await bus.subscribe("risk")
        runtime = MarketRuntime(stream, data_engine, bus)

        bootstrap = await runtime.bootstrap(since=123, max_candles=50)
        assert len(bootstrap.contract_candles) == 2
        assert len(bootstrap.mark_candles) == 2
        assert bootstrap.funding_rows == {"BTC/USDT:USDT": 2}
        assert len(data_engine.sync_calls) == 5
        assert all(identifier != main_thread for identifier in data_engine.sync_threads)

        await runtime.run()
        mark = await subscription.get(timeout=1.0)
        health = await runtime.health_snapshot()
        assert mark.symbol == "BTC/USDT:USDT"
        assert health["runtime"]["published"] == 1
        assert health["runtime"]["running"] is False
        assert health["runtime"]["blocking_calls_completed"] == 6
        assert health["stream"] == {"ok": True}
        assert health["data_engine"] == {"ok": True}
        await runtime.close()

    asyncio.run(scenario())


def test_runtime_records_stream_failures_once() -> None:
    async def scenario() -> None:
        runtime = MarketRuntime(
            FailingStream(),
            FakeDataEngine(),
            MarketEventBus(),
        )
        with pytest.raises(RuntimeError, match="stream failed"):
            await runtime.run()
        assert runtime.health.failures == 1
        assert runtime.health.running is False
        assert runtime.health.stopped_at_ms is not None
        await runtime.close()

    asyncio.run(scenario())


def test_symbol_map_and_bootstrap_inputs_are_strict() -> None:
    expected = {"BTCUSDT": "BTC/USDT:USDT"}
    assert build_binance_symbol_map(["BTC/USDT:USDT"]) == expected
    with pytest.raises(ValueError, match="at least one"):
        build_binance_symbol_map([])
    with pytest.raises(ValueError, match="collide"):
        build_binance_symbol_map(["BTC/USDT:USDT", "BTC/USDT"])
    with pytest.raises(ValueError, match="cover"):
        MarketRuntime(
            FakeStream(),
            FakeDataEngine(),
            MarketEventBus(),
            symbol_map={"ETHUSDT": "ETH/USDT:USDT"},
        )

    async def invalid_since() -> None:
        runtime = MarketRuntime(FakeStream(), FakeDataEngine(), MarketEventBus())
        with pytest.raises(ValueError, match="since"):
            await runtime.bootstrap(since=-1)
        await runtime.close()

    asyncio.run(invalid_since())
