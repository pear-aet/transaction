import asyncio

from crypto_quant.chan import analyze
from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle, Signal
from crypto_quant.market_bus import MarketEventBus
from crypto_quant.market_events import candle_market_event
from crypto_quant.storage import Repository
from crypto_quant.strategy import StrategyContext, StrategyDecision
from crypto_quant.strategy_bus import StrategyIntentBus
from crypto_quant.strategy_engine import StrategyEngine, StrategyEngineSettings
from crypto_quant.strategy_runtime import StrategyRuntime, StrategyRuntimeSettings
from crypto_quant.strategy_store import StrategyStore

LOW_MS = 900_000
HIGH_MS = 3_600_000
SYMBOL = "BTC/USDT:USDT"


def candles(count: int, interval: int) -> list[Candle]:
    return [
        Candle(
            index * interval,
            100 + index * 0.01,
            101 + index * 0.01,
            99 + index * 0.01,
            100.5 + index * 0.01,
            10,
        )
        for index in range(count)
    ]


async def wait_for_bootstrap(engine: StrategyEngine) -> None:
    for _ in range(500):
        if engine.health.bootstrapped:
            return
        await asyncio.sleep(0.002)
    raise AssertionError("strategy runtime did not finish bootstrap")


def test_market_event_bus_to_persisted_strategy_intent_pipeline(tmp_path) -> None:
    async def scenario() -> None:
        repository = Repository(tmp_path / "quant.db")
        low = candles(83, LOW_MS)
        high = candles(20, HIGH_MS)
        repository.store_candles_immutable(SYMBOL, "15m", low)
        repository.store_candles_immutable(SYMBOL, "1h", high)

        calls: list[int] = []

        def evaluator(context: StrategyContext) -> StrategyDecision:
            calls.append(context.decision_time)
            return StrategyDecision(
                Signal(
                    context.low_candles[-1].timestamp,
                    "enter_long",
                    "pipeline_test",
                    context.low_candles[-1].close - 1,
                ),
                analyze(list(context.high_candles)),
                analyze(list(context.low_candles)),
            )

        market_bus = MarketEventBus()
        intent_bus = StrategyIntentBus()
        risk_subscription = await intent_bus.subscribe("risk-v1")
        store = StrategyStore(repository.path)
        config = StrategyConfig(
            symbol=SYMBOL,
            high_timeframe="1h",
            low_timeframe="15m",
            entry_model="chan_fractal",
        )
        engine = StrategyEngine(
            repository,
            {SYMBOL: config},
            StrategyEngineSettings(instance_id="primary"),
            lambda _symbol, _time: None,
            store=store,
            intent_bus=intent_bus,
            decision_function=evaluator,
        )
        runtime = StrategyRuntime(
            market_bus,
            engine,
            StrategyRuntimeSettings("strategy-primary"),
        )
        runtime_task = asyncio.create_task(runtime.run())
        await wait_for_bootstrap(engine)

        next_low = Candle(low[-1].timestamp + LOW_MS, 101, 102, 100, 101.5, 10)
        next_high = Candle(high[-1].timestamp + HIGH_MS, 101, 103, 100, 102, 40)
        repository.store_candles_immutable(SYMBOL, "15m", [next_low])
        repository.store_candles_immutable(SYMBOL, "1h", [next_high])
        low_event = candle_market_event(
            SYMBOL,
            "15m",
            next_low,
            received_at=next_low.timestamp + LOW_MS + 1,
        )
        high_event = candle_market_event(
            SYMBOL,
            "1h",
            next_high,
            received_at=next_high.timestamp + HIGH_MS + 1,
        )

        await market_bus.publish(low_event)
        await asyncio.sleep(0)
        assert calls == []
        await market_bus.publish(high_event)
        produced = await risk_subscription.get(timeout=1.0)

        assert calls == [next_low.timestamp + LOW_MS]
        assert produced.source_event_id == low_event.event_id
        assert produced.storage_sequence == 1
        assert produced.sequence == 1
        assert store.metrics()["published_intents"] == 1

        await runtime.stop()
        await asyncio.wait_for(runtime_task, timeout=1.0)
        await runtime.close()
        await market_bus.close()

    asyncio.run(scenario())
