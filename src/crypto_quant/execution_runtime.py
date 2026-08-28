from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .execution_engine import ExecutionDispatchError, ExecutionEngine
from .execution_store import ExecutionDeliveryState
from .market_bus import BackpressurePolicy
from .risk_bus import RiskDecisionBus, RiskDecisionSubscription
from .risk_contracts import RiskDecision, RiskDecisionFilter, RiskDecisionStatus
from .risk_replay import DurableRiskFilter, RiskReplayLedger
from .risk_store import RiskStore


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeSettings:
    subscription_name: str
    durable_consumer_name: str
    risk_engine_instance_ids: frozenset[str] | None = None
    account_ids: frozenset[str] | None = None
    symbols: frozenset[str] | None = None
    capacity: int = 1024
    replay_batch_size: int = 1
    outbox_flush_interval_ms: int = 100

    def __post_init__(self) -> None:
        if not self.subscription_name or not self.durable_consumer_name:
            raise ValueError("execution runtime consumer names must not be empty")
        if self.capacity <= 0:
            raise ValueError("execution runtime capacity must be positive")
        if self.replay_batch_size != 1:
            raise ValueError(
                "execution runtime replay_batch_size must remain one for atomic recovery"
            )
        if not 1 <= self.outbox_flush_interval_ms <= 60_000:
            raise ValueError("outbox_flush_interval_ms must be in [1, 60000]")

    def decision_filter(self) -> RiskDecisionFilter:
        return RiskDecisionFilter(
            engine_instance_ids=self.risk_engine_instance_ids,
            account_ids=self.account_ids,
            symbols=self.symbols,
            statuses=frozenset({RiskDecisionStatus.APPROVED}),
        )


@dataclass(slots=True)
class ExecutionRuntimeHealth:
    running: bool = False
    started_at_ms: int | None = None
    stopped_at_ms: int | None = None
    bootstrap_runs: int = 0
    replayed_decisions: int = 0
    replay_skipped: int = 0
    live_decisions: int = 0
    periodic_flushes: int = 0
    source_barrier_flushes: int = 0
    failures: int = 0
    last_decision_id: str | None = None
    last_command_id: str | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at_ms": self.started_at_ms,
            "stopped_at_ms": self.stopped_at_ms,
            "bootstrap_runs": self.bootstrap_runs,
            "replayed_decisions": self.replayed_decisions,
            "replay_skipped": self.replay_skipped,
            "live_decisions": self.live_decisions,
            "periodic_flushes": self.periodic_flushes,
            "source_barrier_flushes": self.source_barrier_flushes,
            "failures": self.failures,
            "last_decision_id": self.last_decision_id,
            "last_command_id": self.last_command_id,
            "last_error": self.last_error,
        }


Clock = Callable[[], int]


class ExecutionRuntime:
    """Critical RiskDecision consumer with durable catch-up and outbox recovery."""

    def __init__(
        self,
        decision_bus: RiskDecisionBus,
        risk_store: RiskStore,
        execution_engine: ExecutionEngine,
        settings: ExecutionRuntimeSettings,
        *,
        replay_ledger: RiskReplayLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.decision_bus = decision_bus
        self.risk_store = risk_store
        self.execution_engine = execution_engine
        self.settings = settings
        self.replay_ledger = replay_ledger or RiskReplayLedger(risk_store)
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.health = ExecutionRuntimeHealth()
        self._subscription: RiskDecisionSubscription | None = None

    def _durable_filter(self) -> DurableRiskFilter:
        return DurableRiskFilter(decision_filter=self.settings.decision_filter())

    def _record_failure(self, exc: Exception) -> None:
        self.health.failures += 1
        self.health.last_error = f"{exc.__class__.__name__}: {exc}"

    async def _commit_batch(self, batch, now_ms: int) -> None:
        await asyncio.to_thread(
            self.replay_ledger.commit_batch,
            batch,
            updated_at=now_ms,
        )

    async def _process_decision(self, decision: RiskDecision) -> None:
        result = await self.execution_engine.plan_decision(
            decision,
            now_ms=self.clock(),
        )
        self.health.last_command_id = result.command.command_id
        if result.delivery_state is ExecutionDeliveryState.PENDING:
            await asyncio.sleep(0)
            flushed = await self.execution_engine.flush_outbox(now_ms=self.clock())
            if flushed.blocked_on_unpublished_source:
                self.health.source_barrier_flushes += 1

    async def _catch_up(self) -> None:
        selected_filter = self._durable_filter()
        while True:
            batch = await asyncio.to_thread(
                self.replay_ledger.read_batch,
                self.settings.durable_consumer_name,
                selected_filter,
                limit=self.settings.replay_batch_size,
            )
            if batch.scanned_through == batch.start_sequence and not batch.decisions:
                return
            self.health.replay_skipped += batch.skipped
            if not batch.decisions:
                await self._commit_batch(batch, self.clock())
            else:
                stored = batch.decisions[0]
                try:
                    await self._process_decision(stored.decision)
                except ExecutionDispatchError:
                    persisted = await asyncio.to_thread(
                        self.execution_engine.store.load_by_source_decision,
                        self.execution_engine.settings.engine_instance_id,
                        stored.decision.decision_id,
                    )
                    if persisted is not None:
                        await self._commit_batch(batch, self.clock())
                    raise
                await self._commit_batch(batch, self.clock())
                self.health.replayed_decisions += 1
                self.health.last_decision_id = stored.decision.decision_id
            if batch.scanned_through >= batch.high_watermark:
                return

    async def bootstrap(self) -> None:
        if self._subscription is None:
            self._subscription = await self.decision_bus.subscribe(
                self.settings.subscription_name,
                decision_filter=self.settings.decision_filter(),
                capacity=self.settings.capacity,
                policy=BackpressurePolicy.BLOCK,
                critical=True,
            )
        await self.execution_engine.bootstrap(now_ms=self.clock())
        await self._catch_up()
        self.health.bootstrap_runs += 1
        self.health.last_error = None

    async def _periodic_flush(self) -> None:
        await self.execution_engine.flush_outbox(now_ms=self.clock())
        self.health.periodic_flushes += 1

    async def run(self) -> None:
        if self.health.running:
            raise RuntimeError("execution runtime is already running")
        self.health.running = True
        self.health.started_at_ms = self.clock()
        self.health.stopped_at_ms = None
        self.health.last_error = None
        try:
            await self.bootstrap()
            subscription = self._subscription
            if subscription is None:
                raise RuntimeError("execution runtime bootstrap created no subscription")
            timeout = self.settings.outbox_flush_interval_ms / 1000
            while True:
                try:
                    decision = await subscription.get(timeout=timeout)
                except TimeoutError:
                    await self._periodic_flush()
                    continue
                except StopAsyncIteration:
                    break
                self.health.last_decision_id = decision.decision_id
                await self._process_decision(decision)
                self.health.live_decisions += 1
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
            "execution_engine": await self.execution_engine.health_snapshot(),
            "upstream_replay": await asyncio.to_thread(self.replay_ledger.metrics),
            "decision_bus": await self.decision_bus.health_snapshot(),
        }

    async def close(self) -> None:
        await self.stop()
