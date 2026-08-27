import asyncio

import pytest

from crypto_quant.chan import analyze
from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle, Signal
from crypto_quant.market_events import candle_market_event
from crypto_quant.storage import Repository
from crypto_quant.strategy import StrategyContext, StrategyDecision
from crypto_quant.strategy_bus import StrategyIntentBus
from crypto_quant.strategy_engine import (
    StrategyDeliveryError,
    StrategyEngine,
    StrategyEngineSettings,
    StrategyReplayRequired,
)
from crypto_quant.strategy_store import StrategyStore

LOW_MS = 900_000
HIGH_MS = 3_600_000
SYMBOL = "BTC/USDT:USDT"


def candles(count: int, interval: int, *, start: int = 0) -> list[Candle]:
    return [
        Candle(
            start + index * interval,
            100 + index * 0.01,
            101 + index * 0.01,
            99 + index * 0.01,
            100.5 + index * 0.01,
            10,
        )
        for index in range(count)
    ]


def config() -> StrategyConfig:
    return StrategyConfig(
        symbol=SYMBOL,
        high_timeframe="1h",
        low_timeframe="15m",
        entry_model="chan_fractal",
        entry_lookback=20,
    )


def seed_repository(repository: Repository) -> tuple[list[Candle], list[Candle]]:
    low = candles(83, LOW_MS)
    high = candles(20, HIGH_MS)
    repository.store_candles_immutable(SYMBOL, "15m", low)
    repository.store_candles_immutable(SYMBOL, "1h", high)
    return low, high


def evaluator(calls: list[tuple[int, int, int]]):
    def decide(context: StrategyContext) -> StrategyDecision:
        low_close = context.low_candles[-1].timestamp + LOW_MS
        high_close = context.high_candles[-1].timestamp + HIGH_MS
        calls.append((context.decision_time, low_close, high_close))
        signal = Signal(
            context.low_candles[-1].timestamp,
            "enter_long",
            "deterministic_test_entry",
            context.low_candles[-1].close - 1,
        )
        return StrategyDecision(
            signal,
            analyze(list(context.high_candles)),
            analyze(list(context.low_candles)),
        )

    return decide


def create_engine(
    repository: Repository,
    store: StrategyStore,
    bus: StrategyIntentBus,
    calls: list[tuple[int, int, int]],
) -> StrategyEngine:
    return StrategyEngine(
        repository,
        {SYMBOL: config()},
        StrategyEngineSettings(instance_id="primary"),
        lambda _symbol, _time: None,
        store=store,
        intent_bus=bus,
        decision_function=evaluator(calls),
    )


def event(candle: Candle, timeframe: str, sequence: int):
    interval = LOW_MS if timeframe == "15m" else HIGH_MS
    return candle_market_event(
        SYMBOL,
        timeframe,
        candle,
        source_time=candle.timestamp + interval,
        received_at=candle.timestamp + interval + 1,
    ).with_sequence(sequence)


def test_high_timeframe_barrier_prevents_future_data_and_drains_in_order(tmp_path) -> None:
    async def scenario() -> None:
        repository = Repository(tmp_path / "quant.db")
        low, high = seed_repository(repository)
        store = StrategyStore(repository.path)
        bus = StrategyIntentBus()
        subscription = await bus.subscribe("risk")
        calls: list[tuple[int, int, int]] = []
        engine = create_engine(repository, store, bus, calls)
        await engine.bootstrap()

        first_low = Candle(low[-1].timestamp + LOW_MS, 101, 102, 100, 101.5, 10)
        second_low = Candle(first_low.timestamp + LOW_MS, 101.5, 103, 101, 102, 10)
        boundary_high = Candle(high[-1].timestamp + HIGH_MS, 101, 103, 100, 102, 40)
        repository.store_candles_immutable(SYMBOL, "15m", [first_low, second_low])
        repository.store_candles_immutable(SYMBOL, "1h", [boundary_high])

        await engine.handle_market_event(event(first_low, "15m", 1))
        await engine.handle_market_event(event(second_low, "15m", 2))
        assert calls == []
        assert (await engine.health_snapshot())["engine"]["pending_low_events"][SYMBOL] == 2

        await engine.handle_market_event(event(boundary_high, "1h", 3))
        assert calls == [
            (first_low.timestamp + LOW_MS, first_low.timestamp + LOW_MS, 21 * HIGH_MS),
            (second_low.timestamp + LOW_MS, second_low.timestamp + LOW_MS, 21 * HIGH_MS),
        ]
        first_intent = await subscription.get(timeout=1.0)
        second_intent = await subscription.get(timeout=1.0)
        assert first_intent.decision_time < second_intent.decision_time
        assert first_intent.source_sequence == 1
        assert second_intent.source_sequence == 2
        assert store.metrics()["pending_intents"] == 0
        await engine.close()

    asyncio.run(scenario())


def test_persisted_intent_survives_publish_failure_and_recovers_from_outbox(tmp_path) -> None:
    async def scenario() -> None:
        repository = Repository(tmp_path / "quant.db")
        low, high = seed_repository(repository)
        store = StrategyStore(repository.path)
        first_bus = StrategyIntentBus()
        calls: list[tuple[int, int, int]] = []
        first_engine = create_engine(repository, store, first_bus, calls)
        await first_engine.bootstrap()

        boundary_high = Candle(high[-1].timestamp + HIGH_MS, 101, 103, 100, 102, 40)
        next_low = Candle(low[-1].timestamp + LOW_MS, 101, 102, 100, 101.5, 10)
        repository.store_candles_immutable(SYMBOL, "1h", [boundary_high])
        repository.store_candles_immutable(SYMBOL, "15m", [next_low])
        await first_engine.handle_market_event(event(boundary_high, "1h", 1))
        with pytest.raises(StrategyDeliveryError, match="subscriber"):
            await first_engine.handle_market_event(event(next_low, "15m", 2))
        assert store.metrics()["pending_intents"] == 1
        assert (
            store.load_checkpoint("primary", SYMBOL).last_event_time == next_low.timestamp + LOW_MS
        )
        await first_engine.close()

        second_bus = StrategyIntentBus()
        subscription = await second_bus.subscribe("risk")
        second_engine = create_engine(repository, store, second_bus, [])
        result = await second_engine.bootstrap()
        recovered = await subscription.get(timeout=1.0)
        assert result.recovered_intents == 1
        assert recovered.decision_time == next_low.timestamp + LOW_MS
        assert store.metrics()["pending_intents"] == 0
        await second_engine.close()

    asyncio.run(scenario())


def test_restart_fails_closed_when_market_history_advanced_without_evaluation(tmp_path) -> None:
    async def scenario() -> None:
        repository = Repository(tmp_path / "quant.db")
        low, high = seed_repository(repository)
        store = StrategyStore(repository.path)
        first_bus = StrategyIntentBus()
        first_engine = create_engine(repository, store, first_bus, [])
        await first_engine.bootstrap()
        await first_engine.close()

        repository.store_candles_immutable(
            SYMBOL,
            "15m",
            [Candle(low[-1].timestamp + LOW_MS, 101, 102, 100, 101.5, 10)],
        )
        repository.store_candles_immutable(
            SYMBOL,
            "1h",
            [Candle(high[-1].timestamp + HIGH_MS, 101, 103, 100, 102, 40)],
        )
        second_engine = create_engine(repository, store, StrategyIntentBus(), [])
        with pytest.raises(StrategyReplayRequired, match="unevaluated"):
            await second_engine.bootstrap()
        await second_engine.close()

    asyncio.run(scenario())


def test_strategy_context_rejects_future_high_timeframe_data() -> None:
    low = candles(50, LOW_MS)
    high = candles(20, HIGH_MS)
    decision_time = low[-1].timestamp + LOW_MS
    with pytest.raises(ValueError, match="future data"):
        StrategyContext(tuple(low), tuple(high), config(), decision_time)
