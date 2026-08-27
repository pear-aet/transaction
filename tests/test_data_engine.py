from crypto_quant.data_engine import MarketDataEngine
from crypto_quant.domain import Candle, FundingRate
from crypto_quant.market_stream import ClosedCandleEvent, MarkPriceEvent
from crypto_quant.storage import Repository


class FakeSource:
    def __init__(self) -> None:
        self.candle_calls = []
        self.funding_calls = []

    def fetch_candles(self, symbol, timeframe, since=None, limit=1000, max_candles=5000):
        self.candle_calls.append((symbol, timeframe, since, max_candles))
        rows = [
            Candle(0, 10, 11, 9, 10, 1),
            Candle(60_000, 10, 12, 9, 11, 2),
            Candle(120_000, 11, 13, 10, 12, 3),
            Candle(180_000, 12, 14, 11, 13, 4),
        ]
        selected = [item for item in rows if since is None or item.timestamp >= since]
        return selected[:max_candles]

    fetch_mark_candles = fetch_candles

    def fetch_funding_rates(self, symbol, since=None, limit=1000, max_rates=5000):
        self.funding_calls.append((symbol, since, max_rates))
        values = [FundingRate(10, 0.0001), FundingRate(20, 0.0002)]
        return [item for item in values if since is None or item.timestamp >= since][:max_rates]


def test_incremental_sync_resumes_after_latest_closed_bar(tmp_path) -> None:
    repository = Repository(tmp_path / "quant.db")
    source = FakeSource()
    engine = MarketDataEngine(repository, source)

    first = engine.sync_candles("BTC/USDT:USDT", "1m", since=0)
    assert first.stored == 4

    second = engine.sync_candles("BTC/USDT:USDT", "1m")
    assert second.stored == 0
    assert source.candle_calls[-1][2] == 240_000


def test_live_gap_is_repaired_before_new_candle_is_written(tmp_path) -> None:
    repository = Repository(tmp_path / "quant.db")
    repository.upsert_candles("BTC/USDT:USDT", "1m", [Candle(0, 10, 11, 9, 10, 1)])
    engine = MarketDataEngine(repository, FakeSource())

    event = ClosedCandleEvent(
        "BTCUSDT",
        "1m",
        Candle(180_000, 12, 14, 11, 13, 4),
        240_000,
    )
    engine.ingest_event(event, symbol_map={"BTCUSDT": "BTC/USDT:USDT"})

    candles = repository.load_candles("BTC/USDT:USDT", "1m")
    assert [item.timestamp for item in candles] == [0, 60_000, 120_000, 180_000]
    assert engine.health.gaps_repaired == 1


def test_duplicate_live_candle_is_idempotent(tmp_path) -> None:
    repository = Repository(tmp_path / "quant.db")
    engine = MarketDataEngine(repository, FakeSource())
    event = ClosedCandleEvent("BTCUSDT", "1m", Candle(0, 10, 11, 9, 10, 1), 60_000)

    engine.ingest_event(event, symbol_map={"BTCUSDT": "BTC/USDT:USDT"})
    engine.ingest_event(event, symbol_map={"BTCUSDT": "BTC/USDT:USDT"})

    assert len(repository.load_candles("BTC/USDT:USDT", "1m")) == 1


def test_funding_sync_resumes_after_last_timestamp(tmp_path) -> None:
    repository = Repository(tmp_path / "quant.db")
    source = FakeSource()
    engine = MarketDataEngine(repository, source)

    engine.sync_funding("BTC/USDT:USDT", since=0)
    result = engine.sync_funding("BTC/USDT:USDT")

    assert result == []
    assert source.funding_calls[-1][1] == 21


def test_mark_price_is_kept_as_latest_runtime_snapshot(tmp_path) -> None:
    engine = MarketDataEngine(Repository(tmp_path / "quant.db"), FakeSource())
    event = MarkPriceEvent("BTCUSDT", 100.0, 99.5, 0.0001, 200, 100)
    engine.ingest_event(event, symbol_map={"BTCUSDT": "BTC/USDT:USDT"})
    assert engine.latest_mark_prices["BTC/USDT:USDT"] == event
