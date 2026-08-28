from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .risk import evaluate_risk, validate_risk_decision
from .risk_bus import RiskDecisionBus
from .risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskDecision,
    RiskDecisionStatus,
    RiskPolicy,
)
from .risk_store import RiskDeliveryState, RiskStore
from .strategy_contracts import StrategyIntent


class RiskDeliveryError(RuntimeError):
    """Raised when an approved persisted decision cannot reach execution."""


class RiskSnapshotError(RuntimeError):
    """Raised when an account or market provider returns the wrong partition."""


AccountSnapshotProvider = Callable[[StrategyIntent, int], AccountRiskSnapshot]
MarketSnapshotProvider = Callable[[StrategyIntent, int], MarketRiskSnapshot]
RiskEvaluator = Callable[[RiskContext], RiskDecision]


@dataclass(frozen=True, slots=True)
class RiskEngineSettings:
    engine_instance_id: str
    account_id: str
    outbox_batch_size: int = 1000
    require_intent_storage_sequence: bool = True
    require_execution_subscriber: bool = True

    def __post_init__(self) -> None:
        if not self.engine_instance_id or not self.account_id:
            raise ValueError("risk engine and account identifiers must not be empty")
        if not 1 <= self.outbox_batch_size <= 10_000:
            raise ValueError("outbox_batch_size must be in [1, 10000]")


@dataclass(frozen=True, slots=True)
class RiskBootstrapResult:
    published: int
    expired: int
    remaining_pending: int


@dataclass(frozen=True, slots=True)
class RiskEvaluationResult:
    decision: RiskDecision
    delivery_state: RiskDeliveryState
    duplicate: bool


@dataclass(slots=True)
class RiskEngineHealth:
    bootstrapped: bool = False
    bootstrap_runs: int = 0
    received_intents: int = 0
    duplicate_intents: int = 0
    evaluations: int = 0
    approved: int = 0
    rejected: int = 0
    decisions_persisted: int = 0
    decisions_published: int = 0
    decisions_expired: int = 0
    outbox_recoveries: int = 0
    failures: int = 0
    last_intent_id: str | None = None
    last_decision_id: str | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "bootstrapped": self.bootstrapped,
            "bootstrap_runs": self.bootstrap_runs,
            "received_intents": self.received_intents,
            "duplicate_intents": self.duplicate_intents,
            "evaluations": self.evaluations,
            "approved": self.approved,
            "rejected": self.rejected,
            "decisions_persisted": self.decisions_persisted,
            "decisions_published": self.decisions_published,
            "decisions_expired": self.decisions_expired,
            "outbox_recoveries": self.outbox_recoveries,
            "failures": self.failures,
            "last_intent_id": self.last_intent_id,
            "last_decision_id": self.last_decision_id,
            "last_error": self.last_error,
        }


class RiskEngine:
    """Persistence-first risk evaluator with an expiring execution outbox."""

    def __init__(
        self,
        settings: RiskEngineSettings,
        policy: RiskPolicy,
        account_snapshot_provider: AccountSnapshotProvider,
        market_snapshot_provider: MarketSnapshotProvider,
        *,
        store: RiskStore,
        decision_bus: RiskDecisionBus | None = None,
        evaluator: RiskEvaluator = evaluate_risk,
    ) -> None:
        if account_snapshot_provider is None or market_snapshot_provider is None:
            raise ValueError("risk snapshot providers are required")
        self.settings = settings
        self.policy = policy
        self.account_snapshot_provider = account_snapshot_provider
        self.market_snapshot_provider = market_snapshot_provider
        self.store = store
        self.decision_bus = decision_bus or RiskDecisionBus(
            require_critical_delivery=settings.require_execution_subscriber
        )
        if (
            settings.require_execution_subscriber
            and not self.decision_bus.require_critical_delivery
        ):
            raise ValueError("risk engine requires a critical-delivery decision bus")
        self.evaluator = evaluator
        self.health = RiskEngineHealth()
        self._processing_lock = asyncio.Lock()

    def _record_failure(self, exc: Exception) -> None:
        self.health.failures += 1
        self.health.last_error = f"{exc.__class__.__name__}: {exc}"

    async def _assert_execution_subscriber(self) -> None:
        if not self.settings.require_execution_subscriber:
            return
        health = await self.decision_bus.health_snapshot()
        if health["critical_subscribers"] == 0:
            raise RiskDeliveryError("risk engine has no critical execution subscriber")

    async def _expire_stale_outbox(self, now_ms: int) -> int:
        cursor = 0
        expired = 0
        while True:
            pending = await asyncio.to_thread(
                self.store.pending_decisions,
                engine_instance_id=self.settings.engine_instance_id,
                after_sequence=cursor,
                limit=self.settings.outbox_batch_size,
            )
            if not pending:
                return expired
            for decision in pending:
                if decision.storage_sequence is None:
                    raise RuntimeError("pending risk decision has no storage sequence")
                cursor = decision.storage_sequence
                if now_ms > decision.valid_until:
                    changed = await asyncio.to_thread(
                        self.store.mark_expired,
                        decision.decision_id,
                        now_ms,
                    )
                    if changed:
                        self.health.decisions_expired += 1
                        expired += 1
            if len(pending) < self.settings.outbox_batch_size:
                return expired

    async def _flush_outbox(self, now_ms: int) -> tuple[int, int]:
        expired = await self._expire_stale_outbox(now_ms)
        published = 0
        while True:
            pending = await asyncio.to_thread(
                self.store.pending_decisions,
                engine_instance_id=self.settings.engine_instance_id,
                limit=self.settings.outbox_batch_size,
            )
            if not pending:
                return published, expired
            for decision in pending:
                await self._assert_execution_subscriber()
                result = await self.decision_bus.publish(decision, now_ms=now_ms)
                if result.decision is None and not result.duplicate:
                    raise RiskDeliveryError(
                        f"approved risk decision has no critical delivery: {decision.decision_id}"
                    )
                await asyncio.to_thread(
                    self.store.mark_published,
                    decision.decision_id,
                    now_ms,
                )
                self.health.decisions_published += 1
                published += 1
            if len(pending) < self.settings.outbox_batch_size:
                return published, expired

    async def bootstrap(self, *, now_ms: int | None = None) -> RiskBootstrapResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        async with self._processing_lock:
            try:
                published, expired = await self._flush_outbox(current)
                await self._assert_execution_subscriber()
                self.health.outbox_recoveries += published
                metrics = await asyncio.to_thread(self.store.metrics)
                self.health.bootstrapped = True
                self.health.bootstrap_runs += 1
                self.health.last_error = None
                return RiskBootstrapResult(
                    published,
                    expired,
                    metrics["pending_decisions"],
                )
            except Exception as exc:
                self._record_failure(exc)
                raise

    async def evaluate_intent(
        self,
        intent: StrategyIntent,
        *,
        now_ms: int | None = None,
    ) -> RiskEvaluationResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        if self.settings.require_intent_storage_sequence and intent.storage_sequence is None:
            raise ValueError("risk engine requires a persisted strategy intent")

        async with self._processing_lock:
            self.health.received_intents += 1
            self.health.last_intent_id = intent.intent_id
            try:
                existing = await asyncio.to_thread(
                    self.store.load_by_intent,
                    self.settings.engine_instance_id,
                    intent.intent_id,
                )
                if existing is not None:
                    self.health.duplicate_intents += 1
                    if existing.delivery_state is RiskDeliveryState.PENDING:
                        await self._flush_outbox(current)
                        existing = await asyncio.to_thread(
                            self.store.load_by_intent,
                            self.settings.engine_instance_id,
                            intent.intent_id,
                        )
                        if existing is None:
                            raise RuntimeError("risk decision disappeared after outbox flush")
                    self.health.last_decision_id = existing.decision.decision_id
                    self.health.last_error = None
                    return RiskEvaluationResult(
                        existing.decision,
                        existing.delivery_state,
                        True,
                    )

                account, market = await asyncio.gather(
                    asyncio.to_thread(self.account_snapshot_provider, intent, current),
                    asyncio.to_thread(self.market_snapshot_provider, intent, current),
                )
                if account.account_id != self.settings.account_id:
                    raise RiskSnapshotError(
                        f"account provider returned {account.account_id}, "
                        f"expected {self.settings.account_id}"
                    )
                if market.symbol != intent.symbol:
                    raise RiskSnapshotError(
                        f"market provider returned {market.symbol}, expected {intent.symbol}"
                    )
                context = RiskContext(
                    self.settings.engine_instance_id,
                    intent,
                    account,
                    market,
                    self.policy,
                    current,
                )
                decision = await asyncio.to_thread(self.evaluator, context)
                validate_risk_decision(context, decision)
                self.health.evaluations += 1
                commit = await asyncio.to_thread(
                    self.store.commit_evaluation,
                    decision,
                    updated_at=current,
                )
                if commit.decision_inserted:
                    self.health.decisions_persisted += 1
                stored = commit.stored_decision
                if stored.decision.status is RiskDecisionStatus.APPROVED:
                    self.health.approved += 1
                    await self._flush_outbox(current)
                    refreshed = await asyncio.to_thread(
                        self.store.load_by_intent,
                        self.settings.engine_instance_id,
                        intent.intent_id,
                    )
                    if refreshed is None:
                        raise RuntimeError("risk decision disappeared after publication")
                    stored = refreshed
                else:
                    self.health.rejected += 1
                self.health.last_decision_id = stored.decision.decision_id
                self.health.last_error = None
                return RiskEvaluationResult(
                    stored.decision,
                    stored.delivery_state,
                    commit.duplicate,
                )
            except Exception as exc:
                self._record_failure(exc)
                raise

    async def health_snapshot(self) -> dict[str, Any]:
        store_metrics = await asyncio.to_thread(self.store.metrics)
        return {
            "engine": self.health.snapshot(),
            "store": store_metrics,
            "decision_bus": await self.decision_bus.health_snapshot(),
        }

    async def close(self) -> None:
        await self.decision_bus.close()
