from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .portfolio_contracts import AccountSnapshot
from .portfolio_engine import PortfolioBootstrapResult, PortfolioEngine
from .portfolio_store import PortfolioStore
from .risk_contracts import AccountRiskSnapshot
from .strategy_contracts import StrategyIntent


class PortfolioSnapshotUnavailable(RuntimeError):
    """Raised when Risk Engine cannot receive an authoritative account snapshot."""


Clock = Callable[[], int]


@dataclass(frozen=True, slots=True)
class PortfolioRuntimeSettings:
    poll_interval_seconds: float = 0.25
    snapshot_interval_ms: int = 15_000

    def __post_init__(self) -> None:
        if not 0.01 <= self.poll_interval_seconds <= 60:
            raise ValueError("poll_interval_seconds must be in [0.01, 60]")
        if not 0 <= self.snapshot_interval_ms <= 3_600_000:
            raise ValueError("snapshot_interval_ms must be in [0, 3600000]")


@dataclass(frozen=True, slots=True)
class PortfolioCycleResult:
    bootstrap: PortfolioBootstrapResult
    snapshot: AccountSnapshot | None


@dataclass(slots=True)
class PortfolioRuntimeHealth:
    running: bool = False
    started_at_ms: int | None = None
    stopped_at_ms: int | None = None
    cycles: int = 0
    commands_processed: int = 0
    updates_processed: int = 0
    reservations_released: int = 0
    snapshots_created: int = 0
    failures: int = 0
    last_cycle_at_ms: int | None = None
    last_command_sequence: int = 0
    last_update_sequence: int = 0
    last_snapshot_id: str | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at_ms": self.started_at_ms,
            "stopped_at_ms": self.stopped_at_ms,
            "cycles": self.cycles,
            "commands_processed": self.commands_processed,
            "updates_processed": self.updates_processed,
            "reservations_released": self.reservations_released,
            "snapshots_created": self.snapshots_created,
            "failures": self.failures,
            "last_cycle_at_ms": self.last_cycle_at_ms,
            "last_command_sequence": self.last_command_sequence,
            "last_update_sequence": self.last_update_sequence,
            "last_snapshot_id": self.last_snapshot_id,
            "last_error": self.last_error,
        }


class PortfolioRuntime:
    """Polling durable execution facts into the authoritative account ledger."""

    def __init__(
        self,
        engine: PortfolioEngine,
        settings: PortfolioRuntimeSettings | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings or PortfolioRuntimeSettings()
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.health = PortfolioRuntimeHealth()
        self._stop_event = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self._last_snapshot_at_ms: int | None = None

    def _snapshot_due(self, current: int, bootstrap: PortfolioBootstrapResult) -> bool:
        if self._last_snapshot_at_ms is None:
            return True
        if self.settings.snapshot_interval_ms == 0:
            return True
        if current - self._last_snapshot_at_ms >= self.settings.snapshot_interval_ms:
            return True
        return any(
            (
                bootstrap.commands_processed,
                bootstrap.updates_processed,
                bootstrap.reservations_released,
            )
        )

    async def run_once(self, *, now_ms: int | None = None) -> PortfolioCycleResult:
        current = self.clock() if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        async with self._cycle_lock:
            try:
                bootstrap = await asyncio.to_thread(
                    self.engine.bootstrap,
                    now_ms=current,
                )
                snapshot = None
                if self._snapshot_due(current, bootstrap):
                    snapshot = await asyncio.to_thread(
                        self.engine.snapshot,
                        observed_at=current,
                    )
                    self._last_snapshot_at_ms = current
                    self.health.snapshots_created += 1
                    self.health.last_snapshot_id = snapshot.snapshot_id
                self.health.cycles += 1
                self.health.commands_processed += bootstrap.commands_processed
                self.health.updates_processed += bootstrap.updates_processed
                self.health.reservations_released += bootstrap.reservations_released
                self.health.last_cycle_at_ms = current
                self.health.last_command_sequence = bootstrap.command_high_watermark
                self.health.last_update_sequence = bootstrap.update_high_watermark
                self.health.last_error = None
                return PortfolioCycleResult(bootstrap, snapshot)
            except Exception as exc:
                self.health.failures += 1
                self.health.last_error = f"{exc.__class__.__name__}: {exc}"
                raise

    async def run(self) -> None:
        if self.health.running:
            raise RuntimeError("portfolio runtime is already running")
        self._stop_event.clear()
        self.health.running = True
        self.health.started_at_ms = self.clock()
        self.health.stopped_at_ms = None
        self.health.last_error = None
        try:
            while not self._stop_event.is_set():
                await self.run_once(now_ms=self.clock())
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.settings.poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
        finally:
            self.health.running = False
            self.health.stopped_at_ms = self.clock()

    async def stop(self) -> None:
        self._stop_event.set()

    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "runtime": self.health.snapshot(),
            "portfolio_engine": await asyncio.to_thread(self.engine.health_snapshot),
        }


class PortfolioRiskSnapshotProvider:
    """Read-only adapter from persisted AccountSnapshot to Risk Engine input."""

    def __init__(
        self,
        store: PortfolioStore,
        account_id: str,
        *,
        max_snapshot_age_ms: int = 30_000,
    ) -> None:
        if not account_id:
            raise ValueError("account_id must not be empty")
        if max_snapshot_age_ms < 0:
            raise ValueError("max_snapshot_age_ms must be non-negative")
        self.store = store
        self.account_id = account_id
        self.max_snapshot_age_ms = max_snapshot_age_ms

    def __call__(
        self,
        intent: StrategyIntent,
        evaluated_at: int,
    ) -> AccountRiskSnapshot:
        if evaluated_at < 0:
            raise ValueError("evaluated_at must be non-negative")
        snapshot = self.store.latest_snapshot(self.account_id)
        if snapshot is None:
            raise PortfolioSnapshotUnavailable(
                f"account snapshot is unavailable: {self.account_id}"
            )
        if snapshot.account_id != self.account_id:
            raise PortfolioSnapshotUnavailable("account snapshot identity changed")
        if snapshot.observed_at > evaluated_at:
            raise PortfolioSnapshotUnavailable("account snapshot is from the future")
        if evaluated_at - snapshot.observed_at > self.max_snapshot_age_ms:
            raise PortfolioSnapshotUnavailable("account snapshot is stale")
        if intent.symbol not in {
            position.symbol for position in snapshot.positions
        } and snapshot.global_halt:
            # A halted flat account must still reach Risk Engine so exits and
            # explicit rejection reasons use the same authoritative state.
            return snapshot.to_risk_snapshot()
        return snapshot.to_risk_snapshot()
