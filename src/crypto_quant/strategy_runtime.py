from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .market_bus import BackpressurePolicy, MarketEventBus, MarketSubscription
from .market_events import MarketEventFilter, MarketEventKind
from .strategy_engine import (
    PositionSideProvider,
    RecoveryMode,
    StrategyBootstrapResult,
    StrategyEngine,
)


@dataclass(frozen=True, slots=True)
class StrategyRuntimeSettings:
    subscriber_name: str
    market_queue_capacity: int = 1024

    def __post_init__(self) -> None:
        if not self.subscriber_name:
            raise ValueError("strategy runtime subscriber_name must not be empty")
        if self.market_queue_capacity <= 0:
            raise ValueError("market_queue_capacity must be positive")


@dataclass(slots=True)
class StrategyRuntimeHealth:
    running: bool = False
    started_at_ms: int | None = None
    stopped_at_ms: int | None = None
    consumed_events: int = 0
    failures: int = 0
    last_sequence: int = 0
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at_ms": self.started_at_ms,
            "stopped_at_ms": self.stopped_at_ms,
            "consumed_events": self.consumed_events,
            "failures": self.failures,
            "last_sequence": self.last_sequence,
            "last_error": self.last_error,
        }


class StrategyRuntime:
    """Critical blocking subscriber connecting MarketEventBus to StrategyEngine."""

    def __init__(
        self,
        market_bus: MarketEventBus,
        engine: StrategyEngine,
        settings: StrategyRuntimeSettings,
    ) -> None:
        self.market_bus = market_bus
        self.engine = engine
        self.settings = settings
        self.health = StrategyRuntimeHealth()
        self._subscription: MarketSubscription | None = None
        self._run_lock = asyncio.Lock()

    async def _subscribe(self) -> MarketSubscription:
        symbols = frozenset(self.engine.configs)
        timeframes = frozenset(
            timeframe
            for config in self.engine.configs.values()
            for timeframe in (config.high_timeframe, config.low_timeframe)
        )
        return await self.market_bus.subscribe(
            self.settings.subscriber_name,
            event_filter=MarketEventFilter(
                kinds=frozenset({MarketEventKind.CANDLE_CLOSED}),
                symbols=symbols,
                timeframes=timeframes,
                price_types=frozenset({"contract"}),
            ),
            capacity=self.settings.market_queue_capacity,
            policy=BackpressurePolicy.BLOCK,
        )

    async def run(
        self,
        *,
        recovery_mode: RecoveryMode = RecoveryMode.FAIL,
        historical_position_provider: PositionSideProvider | None = None,
    ) -> StrategyBootstrapResult:
        async with self._run_lock:
            if self.health.running:
                raise RuntimeError("strategy runtime is already running")
            if self.market_bus.closed:
                raise RuntimeError("cannot run against a closed market event bus")
            self._subscription = await self._subscribe()
            self.health.running = True
            self.health.started_at_ms = int(time.time() * 1000)
            self.health.stopped_at_ms = None
            self.health.last_error = None
            try:
                bootstrap = await self.engine.bootstrap(
                    recovery_mode=recovery_mode,
                    historical_position_provider=historical_position_provider,
                )
                async for event in self._subscription:
                    await self.engine.handle_market_event(event)
                    self.health.consumed_events += 1
                    self.health.last_sequence = event.sequence
                return bootstrap
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.failures += 1
                self.health.last_error = f"{exc.__class__.__name__}: {exc}"
                raise
            finally:
                self.health.running = False
                self.health.stopped_at_ms = int(time.time() * 1000)
                if self._subscription is not None:
                    await self._subscription.close()
                    self._subscription = None

    async def stop(self) -> None:
        if self._subscription is not None:
            await self._subscription.close()
            self._subscription = None

    async def close(self) -> None:
        await self.stop()
        await self.engine.close()

    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "runtime": self.health.snapshot(),
            "strategy": await self.engine.health_snapshot(),
        }
