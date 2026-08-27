from crypto_quant.domain import Candle
from crypto_quant.engine import EngineState
from crypto_quant.storage import Repository


def test_sqlite_market_data_and_state_are_idempotent(tmp_path) -> None:
    repository = Repository(tmp_path / "quant.db")
    candles = [Candle(0, 10, 11, 9, 10, 1), Candle(60_000, 10, 12, 9, 11, 2)]
    repository.upsert_candles("BTC/USDT:USDT", "1m", candles)
    repository.upsert_candles("BTC/USDT:USDT", "1m", candles)
    assert repository.load_candles("BTC/USDT:USDT", "1m") == candles

    state = EngineState(10_000, global_stop=True)
    repository.save_engine_state("paper", state, 1)
    restored = repository.load_engine_state("paper", 1)
    assert restored.cash == 10_000
    assert restored.global_stop is True


def test_event_id_prevents_duplicate_replay(tmp_path) -> None:
    repository = Repository(tmp_path / "quant.db")
    repository.append_event("paper", 1, "signal", {"a": 1}, "stable-id")
    repository.append_event("paper", 1, "signal", {"a": 1}, "stable-id")
    with repository._connect() as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_only_one_strategy_version_is_enabled_per_symbol(tmp_path) -> None:
    repository = Repository(tmp_path / "quant.db")
    repository.save_strategy_version("BTC", "a", "1", {}, {}, 10, True)
    repository.save_strategy_version("BTC", "b", "1", {}, {}, 20, True)
    versions = repository.list_strategy_versions("BTC")
    assert sum(item["enabled"] for item in versions) == 1
    assert next(item for item in versions if item["enabled"])["strategy_id"] == "b"


def test_recent_jobs_are_returned_newest_first(tmp_path) -> None:
    repository = Repository(tmp_path / "quant.db")
    first = repository.create_job("backtest", {"symbol": "BTC"}, 1)
    second = repository.create_job("select", {"symbol": "ETH"}, 2)
    jobs = repository.list_jobs()
    assert [item["id"] for item in jobs] == [second, first]
