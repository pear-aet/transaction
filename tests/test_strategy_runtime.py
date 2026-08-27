import asyncio
from types import SimpleNamespace

from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle
from crypto_quant.market_bus import MarketEventBus
from crypto_quant.market_events import candle_market_event
from crypto_quant.strategy_runtime import StrategyRuntime, StrategyRuntimeSettings

SYMBOL = "BTC/USDT:USDT"


class FakeEngine:
    def __init__(self) -> None:
        self.configs = {
            SYMBOL: StrategyConfig(
                symbol=SYMBOL,
                high_timeframe="1h",
                low_timeframe="15m",
            )
        }
        self.bootstrap_started = asyncio.Event()
        self.release_bootstrap = asyncio.Event()
        self.handled = asyncio.Event()
        self.events = []
        self.closed = False

    async def bootstrap(self, **_kwargs):
        self.bootstrap_started.set()
        await self.release_bootstrap.wait()
        return SimpleNamespace(symbols=(), recovered_intents=0)

    async def handle_market_event(self, event) -> None:
        self.events.append(event)
        self.handled.set()

    async def health_snapshot(self):
        return {"fake": True}

    async def close(self) -> None:
        self.closed = True


def test_runtime_subscribes_before_bootstrap_and_does_not_lose_queued_close() -> None:
    async def scenario() -> None:
        market_bus = MarketEventBus()
        engine = FakeEngine()
        runtime = StrategyRuntime(
            market_bus,
            engine,
            StrategyRuntimeSettings("strategy-primary", market_queue_capacity=2),
        )
        task = asyncio.create_task(runtime.run())
        await engine.bootstrap_started.wait()

        event = candle_market_event(
            SYMBOL,
            "15m",
            Candle(0, 100, 102, 99, 101, 10),
            received_at=900_001,
        )
        published = await market_bus.publish(event)
        assert published.event is not None
        engine.release_bootstrap.set()
        await asyncio.wait_for(engine.handled.wait(), timeout=1.0)
        assert [item.sequence for item in engine.events] == [1]

        await runtime.stop()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result.recovered_intents == 0
        health = await runtime.health_snapshot()
        assert health["runtime"]["consumed_events"] == 1
        await runtime.close()
        assert engine.closed is True
        await market_bus.close()

    asyncio.run(scenario())
