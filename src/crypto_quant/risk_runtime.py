from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .market_bus import BackpressurePolicy
from .risk_engine import RiskDeliveryError, RiskEngine
from .strategy_bus import StrategyIntentBus, StrategyIntentSubscription
from .strategy_contracts import StrategyIntentFilter
from .strategy_replay import DurableIntentFilter, StrategyReplayLedger
from .strategy_store import StrategyStore


@dataclass(frozen=True, slots=True)
class RiskRuntimeSettings:
    subscription_name: str
    durable_consumer_name: str
    strategy_instance_ids: frozenset[str] | None = None
    symbols: frozenset[str] | None = None
    capacity: int = 1024
    replay_batch_size: int = 1

    def __post_init__(self) -> None:
        if not self.subscription_name or not self.durable_consumer_name:
            raise ValueError("risk runtime consumer names must not be empty")
        if self.capacity <= 0:
            raise ValueError("risk runtime capacity must be positive")
        if self.replay_batch_size != 1:
            raise ValueError("risk runtime replay_batch_size must remain one for atomic recovery")

    def intent_filter(self) -> StrategyIntentFilter:
        return StrategyIntentFilter(
            instance_ids=self.strategy_instance_ids,
            symbols=self.symbols,
        )


@dataclass(slots=True)
class RiskRuntimeHealth:
    running: bool = False
    started_at_ms: int | None = None
    stopped_at_ms: int | None = None
    bootstrap_runs: int = 0
    replayed_intents: int = 0
    replay_skipped: int = 0
    live_intents: int = 0
    failures: int = 0
    last_intent_id: str | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at_ms": self.started_at_ms,
            "stopped_at_ms": self.stopped_at_ms,
            "bootstrap_runs": self.bootstrap_runs,
            "replayed_intents": self.replayed_intents,
            "replay_skipped": self.replay_skipped,
            "live_intents": self.live_intents,
            "failures": self.failures,
            "last_intent_id": self.last_intent_id,
            "last_error": self.last_error,
        }


Clock = Callable[[], int]


class RiskRuntime:
    """Critical live StrategyIntent consumer with durable startup catch-up."""

    def __init__(
        self,
        intent_bus: StrategyIntentBus,
        strategy_store: StrategyStore,
        risk_engine: RiskEngine,
        settings: RiskRuntimeSettings,
        *,
        replay_ledger: StrategyReplayLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.intent_bus = intent_bus
        self.strategy_store = strategy_store
        self.risk_engine = risk_engine
        self.settings = settings
        self.replay_ledger = replay_ledger or StrategyReplayLedger(strategy_store)
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.health = RiskRuntimeHealth()
        self._subscription: StrategyIntentSubscription | None = None

    def _durable_filter(self) -> DurableIntentFilter:
        return DurableIntentFilter(intent_filter=self.settings.intent_filter())

    def _record_failure(self, exc: Exception) -> None:
        self.health.failures += 1
        self.health.last_error = f"{exc.__class__.__name__}: {exc}"

    async def _commit_batch(self, batch, now_ms: int) -> None:
        await asyncio.to_thread(
            self.replay_ledger.commit_batch,
            batch,
            updated_at=now_ms,
        )

    async def _catch_up(self) -> None:
        selected_filter = self._durable_filter()
        while True:
            batch = await asyncio.to_thread(
                self.replay_ledger.read_batch,
                self.settings.durable_consumer_name,
                selected_filter,
                limit=self.settings.replay_batch_size,
            )
            if batch.scanned_through == batch.start_sequence and not batch.intents:
                return
            self.health.replay_skipped += batch.skipped
            if not batch.intents:
                await self._commit_batch(batch, self.clock())
            else:
                stored = batch.intents[0]
                try:
                    await self.risk_engine.evaluate_intent(
                        stored.intent,
                        now_ms=self.clock(),
                    )
                except RiskDeliveryError:
                    persisted = await asyncio.to_thread(
                        self.risk_engine.store.load_by_intent,
                        self.risk_engine.settings.engine_instance_id,
                        stored.intent.intent_id,
                    )
                    if persisted is not None:
                        await self._commit_batch(batch, self.clock())
                    raise
                await self._commit_batch(batch, self.clock())
                self.health.replayed_intents += 1
                self.health.last_intent_id = stored.intent.intent_id
            if batch.scanned_through >= batch.high_watermark:
                return

    async def bootstrap(self) -> None:
        if self._subscription is None:
            self._subscription = await self.intent_bus.subscribe(
                self.settings.subscription_name,
                intent_filter=self.settings.intent_filter(),
                capacity=self.settings.capacity,
                policy=BackpressurePolicy.BLOCK,
                critical=True,
            )
        await self.risk_engine.bootstrap(now_ms=self.clock())
        await self._catch_up()
        self.health.bootstrap_runs += 1
        self.health.last_error = None

    async def run(self) -> None:
        if self.health.running:
            raise RuntimeError("risk runtime is already running")
        self.health.running = True
        self.health.started_at_ms = self.clock()
        self.health.stopped_at_ms = None
        self.health.last_error = None
        try:
            await self.bootstrap()
            if self._subscription is None:
                raise RuntimeError("risk runtime bootstrap created no subscription")
            async for intent in self._subscription:
                self.health.last_intent_id = intent.intent_id
                await self.risk_engine.evaluate_intent(intent, now_ms=self.clock())
                self.health.live_intents += 1
        except Exception as exc:
            self._record_failure(exc)
            raise
        finally:
            self.health.running = False
            self.health.stopped_at_ms = self.clock()

    async def stop(self) -> None:
        if self._subscription is not None:
            await self._subscription.close()
            self._subscription = None

    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "runtime": self.health.snapshot(),
            "risk_engine": await self.risk_engine.health_snapshot(),
            "upstream_replay": await asyncio.to_thread(self.replay_ledger.metrics),
        }

    async def close(self) -> None:
        await self.stop()
        await self.risk_engine.close()
