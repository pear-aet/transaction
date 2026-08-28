from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .execution import build_execution_command, validate_execution_command
from .execution_broker import ExecutionBroker
from .execution_contracts import (
    ExecutionCommand,
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    OrderLegType,
    OrderState,
    OrderUpdate,
)
from .execution_store import (
    ExecutionDeliveryState,
    ExecutionStore,
    StoredExecutionCommand,
)
from .risk_contracts import RiskDecision, RiskDecisionStatus
from .risk_store import RiskDeliveryState, RiskStore, StoredRiskDecision


class ExecutionSourceError(RuntimeError):
    """Raised when a command cannot be tied to an immutable risk-store fact."""


class ExecutionDispatchError(RuntimeError):
    """Raised when a persisted simulation command cannot reach the local broker."""


MarketSnapshotProvider = Callable[[RiskDecision, int], ExecutionMarketSnapshot]
ExecutionPlanner = Callable[[ExecutionContext], ExecutionCommand]


@dataclass(frozen=True, slots=True)
class ExecutionEngineSettings:
    engine_instance_id: str
    account_id: str
    outbox_batch_size: int = 1000

    def __post_init__(self) -> None:
        if not self.engine_instance_id or not self.account_id:
            raise ValueError("execution engine and account identifiers must not be empty")
        if not 1 <= self.outbox_batch_size <= 10_000:
            raise ValueError("outbox_batch_size must be in [1, 10000]")


@dataclass(frozen=True, slots=True)
class ExecutionBootstrapResult:
    dispatched: int
    expired: int
    blocked_on_unpublished_source: int
    remaining_pending: int
    reconciled: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionPlanningResult:
    command: ExecutionCommand
    delivery_state: ExecutionDeliveryState
    duplicate: bool


@dataclass(slots=True)
class ExecutionEngineHealth:
    bootstrapped: bool = False
    bootstrap_runs: int = 0
    received_decisions: int = 0
    duplicate_decisions: int = 0
    commands_planned: int = 0
    commands_persisted: int = 0
    shadow_recorded: int = 0
    pending_created: int = 0
    commands_dispatched: int = 0
    commands_expired: int = 0
    commands_completed: int = 0
    commands_failed: int = 0
    source_barriers: int = 0
    order_updates: int = 0
    protection_activations: int = 0
    startup_reconciliations: int = 0
    failures: int = 0
    last_decision_id: str | None = None
    last_command_id: str | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "bootstrapped": self.bootstrapped,
            "bootstrap_runs": self.bootstrap_runs,
            "received_decisions": self.received_decisions,
            "duplicate_decisions": self.duplicate_decisions,
            "commands_planned": self.commands_planned,
            "commands_persisted": self.commands_persisted,
            "shadow_recorded": self.shadow_recorded,
            "pending_created": self.pending_created,
            "commands_dispatched": self.commands_dispatched,
            "commands_expired": self.commands_expired,
            "commands_completed": self.commands_completed,
            "commands_failed": self.commands_failed,
            "source_barriers": self.source_barriers,
            "order_updates": self.order_updates,
            "protection_activations": self.protection_activations,
            "startup_reconciliations": self.startup_reconciliations,
            "failures": self.failures,
            "last_decision_id": self.last_decision_id,
            "last_command_id": self.last_command_id,
            "last_error": self.last_error,
        }


class ExecutionEngine:
    """Persistence-first command planner with a local-only simulation outbox."""

    def __init__(
        self,
        settings: ExecutionEngineSettings,
        policy: ExecutionPolicy,
        market_snapshot_provider: MarketSnapshotProvider,
        *,
        store: ExecutionStore,
        risk_store: RiskStore,
        broker: ExecutionBroker | None = None,
        planner: ExecutionPlanner = build_execution_command,
    ) -> None:
        if market_snapshot_provider is None:
            raise ValueError("execution market snapshot provider is required")
        if policy.mode is ExecutionMode.SHADOW and broker is not None:
            raise ValueError("shadow execution must not have a broker")
        if policy.mode is ExecutionMode.SIMULATION and broker is None:
            raise ValueError("simulation execution requires a local broker")
        self.settings = settings
        self.policy = policy
        self.market_snapshot_provider = market_snapshot_provider
        self.store = store
        self.risk_store = risk_store
        self.broker = broker
        self.planner = planner
        self.health = ExecutionEngineHealth()
        self._processing_lock = asyncio.Lock()

    def _record_failure(self, exc: Exception) -> None:
        self.health.failures += 1
        self.health.last_error = f"{exc.__class__.__name__}: {exc}"

    def _load_input_source(self, decision: RiskDecision) -> StoredRiskDecision:
        sequence = int(decision.storage_sequence or 0)
        rows = self.risk_store.load_decisions(
            after_sequence=max(0, sequence - 1),
            through_sequence=sequence,
            limit=1,
        )
        if len(rows) != 1:
            raise ExecutionSourceError(f"risk decision storage sequence is missing: {sequence}")
        stored = rows[0]
        if stored.decision.storage_sequence != sequence:
            raise ExecutionSourceError("risk decision storage sequence mismatch")
        if stored.decision.semantic_dict() != decision.semantic_dict():
            raise ExecutionSourceError(
                f"risk decision semantics differ from RiskStore: {decision.decision_id}"
            )
        return stored

    def _load_source(self, command: ExecutionCommand) -> StoredRiskDecision:
        sequence = command.source_decision_storage_sequence
        rows = self.risk_store.load_decisions(
            after_sequence=sequence - 1,
            through_sequence=sequence,
            limit=1,
        )
        if len(rows) != 1:
            raise ExecutionSourceError(f"risk decision storage sequence is missing: {sequence}")
        stored = rows[0]
        decision = stored.decision
        mismatches = []
        if decision.decision_id != command.source_decision_id:
            mismatches.append("decision_id")
        if decision.storage_sequence != sequence:
            mismatches.append("storage_sequence")
        if decision.account_id != command.account_id:
            mismatches.append("account_id")
        if decision.symbol != command.symbol:
            mismatches.append("symbol")
        if decision.status is not RiskDecisionStatus.APPROVED:
            mismatches.append("status")
        if decision.approved_action is not command.action:
            mismatches.append("approved_action")
        for name, actual, expected in (
            ("quantity", command.quantity, decision.quantity),
            ("reference_price", command.reference_price, decision.reference_price),
            ("leverage", command.leverage, decision.leverage),
            ("required_margin", command.required_margin, decision.required_margin),
        ):
            if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12):
                mismatches.append(name)
        if command.stop_price != decision.stop_price:
            mismatches.append("stop_price")
        if command.reduce_only != decision.reduce_only:
            mismatches.append("reduce_only")
        if command.valid_until > decision.valid_until:
            mismatches.append("valid_until")
        if command.evidence.get("source_risk_valid_until") != decision.valid_until:
            mismatches.append("source_risk_valid_until")
        if mismatches:
            raise ExecutionSourceError(
                "execution source risk decision mismatch: " + ", ".join(mismatches)
            )
        return stored

    async def _apply_updates(self, updates: tuple[OrderUpdate, ...]) -> int:
        applied = 0
        for update in updates:
            commit = await asyncio.to_thread(self.store.apply_order_update, update)
            if not commit.duplicate:
                applied += 1
                self.health.order_updates += 1
        return applied

    async def _reconcile_command(
        self,
        stored: StoredExecutionCommand,
        now_ms: int,
    ) -> None:
        if stored.delivery_state not in {
            ExecutionDeliveryState.DISPATCHED,
            ExecutionDeliveryState.COMPLETED,
            ExecutionDeliveryState.FAILED,
        }:
            return
        primary = [order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY]
        if len(primary) != 1:
            raise RuntimeError("persisted execution command has no unique primary order")
        primary_order = primary[0]
        protective = [
            order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
        ]

        failed_states = {
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        }
        failure_reason: str | None = None
        if primary_order.state in failed_states:
            failure_reason = f"primary_{primary_order.state.value}"
        elif primary_order.state is OrderState.CANCELED:
            if primary_order.cumulative_filled_quantity <= 0:
                failure_reason = "primary_canceled_without_fill"
        if protective:
            protective_order = protective[0]
            if protective_order.state in failed_states:
                failure_reason = f"protective_{protective_order.state.value}"
            elif protective_order.state is OrderState.CANCELED:
                if primary_order.cumulative_filled_quantity > 0:
                    failure_reason = "protective_canceled_with_open_fill"

        if failure_reason is not None:
            if stored.delivery_state is ExecutionDeliveryState.DISPATCHED:
                changed = await asyncio.to_thread(
                    self.store.mark_failed,
                    stored.command.command_id,
                    now_ms,
                    failure_reason,
                    evidence={"source": "order_reconciliation"},
                )
                if changed:
                    self.health.commands_failed += 1
            return

        completed = False
        if not protective:
            completed = primary_order.state is OrderState.FILLED
        else:
            protective_order = protective[0]
            completed = (
                primary_order.state is OrderState.FILLED
                and protective_order.state is OrderState.FILLED
            )
        if completed and stored.delivery_state is ExecutionDeliveryState.DISPATCHED:
            changed = await asyncio.to_thread(
                self.store.mark_completed,
                stored.command.command_id,
                now_ms,
                evidence={"source": "order_reconciliation"},
            )
            if changed:
                self.health.commands_completed += 1

    async def _dispatch_command(
        self,
        stored: StoredExecutionCommand,
        now_ms: int,
    ) -> bool:
        command = stored.command
        submitted_orders = [
            order for order in stored.orders if order.state is not OrderState.PLANNED
        ]
        if submitted_orders:
            recovered_dispatch_time = max(order.updated_at for order in submitted_orders)
            if recovered_dispatch_time > command.valid_until:
                raise ExecutionDispatchError(
                    "persisted order update occurred after command validity"
                )
            changed = await asyncio.to_thread(
                self.store.mark_dispatched,
                command.command_id,
                recovered_dispatch_time,
                evidence={"source": "pending_order_recovery"},
            )
            if changed:
                self.health.commands_dispatched += 1
            refreshed = await asyncio.to_thread(
                self.store.load_command,
                command.command_id,
            )
            if refreshed is None:
                raise RuntimeError("execution command disappeared during dispatch recovery")
            await self._reconcile_command(refreshed, now_ms)
            return True
        if now_ms > command.valid_until:
            changed = await asyncio.to_thread(
                self.store.mark_expired,
                command.command_id,
                now_ms,
                evidence={"source": "outbox_expiry"},
            )
            if changed:
                self.health.commands_expired += 1
            return True

        source = await asyncio.to_thread(self._load_source, command)
        if source.delivery_state is RiskDeliveryState.PENDING:
            self.health.source_barriers += 1
            return False
        if source.delivery_state is RiskDeliveryState.EXPIRED:
            changed = await asyncio.to_thread(
                self.store.mark_expired,
                command.command_id,
                now_ms,
            )
            if changed:
                self.health.commands_expired += 1
            return True
        if source.delivery_state is not RiskDeliveryState.PUBLISHED:
            raise ExecutionSourceError(
                f"approved execution source is not published: {source.delivery_state.value}"
            )
        if self.broker is None:
            raise ExecutionDispatchError("simulation outbox has no local broker")

        try:
            updates = await asyncio.to_thread(
                self.broker.dispatch,
                command,
                stored.orders,
                now_ms,
            )
            await self._apply_updates(updates)
            changed = await asyncio.to_thread(
                self.store.mark_dispatched,
                command.command_id,
                now_ms,
                evidence={"source": "local_simulation_broker"},
            )
            if changed:
                self.health.commands_dispatched += 1
            refreshed = await asyncio.to_thread(self.store.load_command, command.command_id)
            if refreshed is None:
                raise RuntimeError("execution command disappeared after dispatch")
            await self._reconcile_command(refreshed, now_ms)
            return True
        except Exception as exc:
            raise ExecutionDispatchError(
                f"local simulation dispatch failed: {command.command_id}"
            ) from exc

    async def _reconcile_active_commands(self, now_ms: int) -> int:
        reconciled = 0
        after_sequence = 0
        while True:
            active = await asyncio.to_thread(
                self.store.active_commands,
                engine_instance_id=self.settings.engine_instance_id,
                after_sequence=after_sequence,
                limit=self.settings.outbox_batch_size,
            )
            if not active:
                break
            for stored in active:
                await self._reconcile_command(stored, now_ms)
                reconciled += 1
                sequence = stored.command.storage_sequence
                if sequence is None:
                    raise RuntimeError("active execution command has no storage sequence")
                after_sequence = sequence
            if len(active) < self.settings.outbox_batch_size:
                break
        self.health.startup_reconciliations += reconciled
        return reconciled

    async def _flush_outbox_unlocked(
        self,
        *,
        now_ms: int | None = None,
    ) -> ExecutionBootstrapResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        dispatched_before = self.health.commands_dispatched
        expired_before = self.health.commands_expired
        blocked = 0
        while True:
            pending = await asyncio.to_thread(
                self.store.pending_commands,
                engine_instance_id=self.settings.engine_instance_id,
                limit=self.settings.outbox_batch_size,
            )
            if not pending:
                break
            for stored in pending:
                progressed = await self._dispatch_command(stored, current)
                if not progressed:
                    blocked += 1
                    remaining = await asyncio.to_thread(self.store.metrics)
                    return ExecutionBootstrapResult(
                        self.health.commands_dispatched - dispatched_before,
                        self.health.commands_expired - expired_before,
                        blocked,
                        remaining["pending_commands"],
                    )
            if len(pending) < self.settings.outbox_batch_size:
                break
        remaining = await asyncio.to_thread(self.store.metrics)
        return ExecutionBootstrapResult(
            self.health.commands_dispatched - dispatched_before,
            self.health.commands_expired - expired_before,
            blocked,
            remaining["pending_commands"],
        )

    async def flush_outbox(
        self,
        *,
        now_ms: int | None = None,
    ) -> ExecutionBootstrapResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        async with self._processing_lock:
            try:
                result = await self._flush_outbox_unlocked(now_ms=current)
                self.health.last_error = None
                return result
            except Exception as exc:
                self._record_failure(exc)
                raise

    async def bootstrap(self, *, now_ms: int | None = None) -> ExecutionBootstrapResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        async with self._processing_lock:
            try:
                result = await self._flush_outbox_unlocked(now_ms=current)
                reconciled = await self._reconcile_active_commands(current)
                self.health.bootstrapped = True
                self.health.bootstrap_runs += 1
                self.health.last_error = None
                return ExecutionBootstrapResult(
                    result.dispatched,
                    result.expired,
                    result.blocked_on_unpublished_source,
                    result.remaining_pending,
                    reconciled,
                )
            except Exception as exc:
                self._record_failure(exc)
                raise

    async def plan_decision(
        self,
        decision: RiskDecision,
        *,
        now_ms: int | None = None,
    ) -> ExecutionPlanningResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        if decision.status is not RiskDecisionStatus.APPROVED:
            raise ValueError("execution engine accepts approved risk decisions only")
        if decision.storage_sequence is None:
            raise ValueError("execution engine requires a persisted risk decision")
        if decision.account_id != self.settings.account_id:
            raise ExecutionSourceError(
                f"risk decision account {decision.account_id} does not match "
                f"execution account {self.settings.account_id}"
            )

        async with self._processing_lock:
            self.health.received_decisions += 1
            self.health.last_decision_id = decision.decision_id
            try:
                await asyncio.to_thread(self._load_input_source, decision)
                existing = await asyncio.to_thread(
                    self.store.load_by_source_decision,
                    self.settings.engine_instance_id,
                    decision.decision_id,
                )
                if existing is not None:
                    self.health.duplicate_decisions += 1
                    if existing.delivery_state is ExecutionDeliveryState.PENDING:
                        await self._flush_outbox_unlocked(now_ms=current)
                        existing = await asyncio.to_thread(
                            self.store.load_by_source_decision,
                            self.settings.engine_instance_id,
                            decision.decision_id,
                        )
                        if existing is None:
                            raise RuntimeError("execution command disappeared after recovery")
                    self.health.last_command_id = existing.command.command_id
                    self.health.last_error = None
                    return ExecutionPlanningResult(
                        existing.command,
                        existing.delivery_state,
                        True,
                    )

                market = await asyncio.to_thread(
                    self.market_snapshot_provider,
                    decision,
                    current,
                )
                if market.symbol != decision.symbol:
                    raise ExecutionSourceError(
                        f"execution market returned {market.symbol}, expected {decision.symbol}"
                    )
                context = ExecutionContext(
                    self.settings.engine_instance_id,
                    decision,
                    market,
                    self.policy,
                    current,
                )
                command = await asyncio.to_thread(self.planner, context)
                validate_execution_command(context, command)
                self.health.commands_planned += 1
                commit = await asyncio.to_thread(
                    self.store.commit_command,
                    command,
                    updated_at=current,
                )
                if commit.command_inserted:
                    self.health.commands_persisted += 1
                stored = commit.stored_command
                if stored.delivery_state is ExecutionDeliveryState.RECORDED:
                    self.health.shadow_recorded += 1
                elif stored.delivery_state is ExecutionDeliveryState.PENDING:
                    self.health.pending_created += 1
                    await self._flush_outbox_unlocked(now_ms=current)
                    refreshed = await asyncio.to_thread(
                        self.store.load_command,
                        stored.command.command_id,
                    )
                    if refreshed is None:
                        raise RuntimeError("execution command disappeared after outbox flush")
                    stored = refreshed
                elif stored.delivery_state is ExecutionDeliveryState.EXPIRED:
                    self.health.commands_expired += 1
                elif stored.delivery_state is ExecutionDeliveryState.FAILED:
                    self.health.commands_failed += 1
                self.health.last_command_id = stored.command.command_id
                self.health.last_error = None
                return ExecutionPlanningResult(
                    stored.command,
                    stored.delivery_state,
                    commit.duplicate,
                )
            except Exception as exc:
                self._record_failure(exc)
                raise

    async def record_order_update(self, update: OrderUpdate) -> StoredExecutionCommand:
        async with self._processing_lock:
            try:
                existing_update = await asyncio.to_thread(
                    self.store.load_order_update,
                    update.update_id,
                )
                if existing_update is None:
                    current_order = await asyncio.to_thread(
                        self.store.load_order,
                        update.order_id,
                    )
                    if current_order is None:
                        raise KeyError(f"execution order not found: {update.order_id}")
                    current_command = await asyncio.to_thread(
                        self.store.load_command,
                        current_order.command_id,
                    )
                    if current_command is None:
                        raise RuntimeError("execution order has no persisted command")
                    if (
                        current_command.command.engine_instance_id
                        != self.settings.engine_instance_id
                    ):
                        raise ExecutionSourceError("order belongs to another execution instance")
                    if current_command.command.mode is not ExecutionMode.SIMULATION:
                        raise ExecutionDispatchError("shadow command cannot receive order updates")
                    if current_command.delivery_state is not ExecutionDeliveryState.DISPATCHED:
                        raise ExecutionDispatchError(
                            "new order updates require a dispatched simulation command"
                        )
                commit = await asyncio.to_thread(self.store.apply_order_update, update)
                if not commit.duplicate:
                    self.health.order_updates += 1
                stored = await asyncio.to_thread(
                    self.store.load_command,
                    commit.order.command_id,
                )
                if stored is None:
                    raise RuntimeError("updated order has no execution command")

                if (
                    self.policy.mode is ExecutionMode.SIMULATION
                    and self.broker is not None
                    and commit.order.leg_type is OrderLegType.PRIMARY
                    and commit.order.state
                    in {
                        OrderState.PARTIALLY_FILLED,
                        OrderState.FILLED,
                    }
                ):
                    activation = await asyncio.to_thread(
                        self.broker.activate_protection,
                        stored.command,
                        stored.orders,
                        update.event_time,
                    )
                    applied = await self._apply_updates(activation)
                    if applied:
                        self.health.protection_activations += 1
                        stored = await asyncio.to_thread(
                            self.store.load_command,
                            commit.order.command_id,
                        )
                        if stored is None:
                            raise RuntimeError(
                                "execution command disappeared after protection activation"
                            )
                await self._reconcile_command(stored, update.event_time)
                refreshed = await asyncio.to_thread(
                    self.store.load_command,
                    commit.order.command_id,
                )
                if refreshed is None:
                    raise RuntimeError("execution command disappeared after reconciliation")
                self.health.last_command_id = refreshed.command.command_id
                self.health.last_error = None
                return refreshed
            except Exception as exc:
                self._record_failure(exc)
                raise

    async def health_snapshot(self) -> dict[str, Any]:
        store_metrics = await asyncio.to_thread(self.store.metrics)
        oldest = await asyncio.to_thread(self.store.oldest_pending_created_at)
        now_ms = int(time.time() * 1000)
        return {
            "engine": self.health.snapshot(),
            "store": store_metrics,
            "oldest_pending_age_ms": None if oldest is None else max(0, now_ms - oldest),
            "mode": self.policy.mode.value,
            "network_write_enabled": False,
        }
