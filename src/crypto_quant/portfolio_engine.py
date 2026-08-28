from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .domain import MarketRules
from .execution_contracts import (
    ExecutionCommand,
    ExecutionCommandStatus,
    ExecutionMode,
    ExecutionOrder,
    OrderLegType,
)
from .execution_store import (
    ExecutionDeliveryState,
    ExecutionStore,
    StoredExecutionCommand,
    StoredOrderUpdate,
)
from .portfolio_accounting import (
    PortfolioAccountingError,
    apply_close_fill,
    apply_funding_to_position,
    apply_mark_fact,
    apply_open_fill,
    build_account_snapshot,
    initialize_account_transaction,
    plan_command_reservation,
    release_command_reservation,
)
from .portfolio_contracts import (
    ZERO,
    AccountHaltReason,
    AccountPolicy,
    AccountSnapshot,
    AccountWatermarks,
    CommandReservation,
    PositionSide,
    ReservationState,
    as_decimal,
)
from .portfolio_facts import (
    ReconciliationKind,
    ReconciliationSeverity,
    make_mark_price_fact,
    make_reconciliation_finding,
)
from .portfolio_store import (
    AccountStatus,
    FillCommit,
    MarkCommit,
    PortfolioStore,
    ReservationCommit,
    StoredExecutionDisposition,
)
from .risk_contracts import RiskApprovedAction, RiskDecision, RiskDecisionStatus
from .risk_store import RiskStore


class PortfolioSourceError(RuntimeError):
    """Raised when an account event is not an exact durable upstream fact."""


class PortfolioBarrierError(RuntimeError):
    """Raised when an order update overtakes its command reservation."""


class PortfolioHealthError(RuntimeError):
    """Raised when a healthy account snapshot cannot be produced."""


MaintenanceRateProvider = Callable[[str, Decimal], Decimal | int | float | str]
CorrelationGroupProvider = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class PortfolioEngineSettings:
    account_id: str
    execution_engine_instance_ids: frozenset[str] | None = None
    command_batch_size: int = 1
    update_batch_size: int = 1

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("portfolio account_id must not be empty")
        if self.execution_engine_instance_ids is not None and not self.execution_engine_instance_ids:
            raise ValueError("execution_engine_instance_ids cannot be empty")
        if self.command_batch_size != 1 or self.update_batch_size != 1:
            raise ValueError("portfolio durable batches must remain one for atomic accounting")


@dataclass(frozen=True, slots=True)
class CommandAccountingResult:
    command_id: str
    source_sequence: int
    disposition: str
    reservation: CommandReservation | None
    duplicate: bool


@dataclass(frozen=True, slots=True)
class UpdateAccountingResult:
    update_id: str
    source_sequence: int
    disposition: str
    fill_commit: FillCommit | None
    duplicate: bool


@dataclass(frozen=True, slots=True)
class PortfolioBootstrapResult:
    commands_processed: int
    updates_processed: int
    reservations_released: int
    command_high_watermark: int
    update_high_watermark: int


@dataclass(slots=True)
class PortfolioEngineHealth:
    initialized: bool = False
    commands_received: int = 0
    commands_processed: int = 0
    command_duplicates: int = 0
    updates_received: int = 0
    updates_processed: int = 0
    update_duplicates: int = 0
    fills_accounted: int = 0
    no_accounting_updates: int = 0
    reservations_released: int = 0
    marks_applied: int = 0
    funding_applied: int = 0
    snapshots_created: int = 0
    barriers: int = 0
    reconciliation_failures: int = 0
    failures: int = 0
    last_command_id: str | None = None
    last_update_id: str | None = None
    last_snapshot_id: str | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "initialized": self.initialized,
            "commands_received": self.commands_received,
            "commands_processed": self.commands_processed,
            "command_duplicates": self.command_duplicates,
            "updates_received": self.updates_received,
            "updates_processed": self.updates_processed,
            "update_duplicates": self.update_duplicates,
            "fills_accounted": self.fills_accounted,
            "no_accounting_updates": self.no_accounting_updates,
            "reservations_released": self.reservations_released,
            "marks_applied": self.marks_applied,
            "funding_applied": self.funding_applied,
            "snapshots_created": self.snapshots_created,
            "barriers": self.barriers,
            "reconciliation_failures": self.reconciliation_failures,
            "failures": self.failures,
            "last_command_id": self.last_command_id,
            "last_update_id": self.last_update_id,
            "last_snapshot_id": self.last_snapshot_id,
            "last_error": self.last_error,
        }


class PortfolioEngine:
    """Authoritative simulation account writer over durable execution facts."""

    def __init__(
        self,
        settings: PortfolioEngineSettings,
        policy: AccountPolicy,
        *,
        store: PortfolioStore,
        execution_store: ExecutionStore,
        risk_store: RiskStore,
        maintenance_rate_provider: MaintenanceRateProvider,
        correlation_group_provider: CorrelationGroupProvider | None = None,
    ) -> None:
        if store.path != execution_store.path or store.path != risk_store.path:
            raise ValueError(
                "Portfolio, Execution and Risk stores must share one SQLite database"
            )
        if policy.mode.value not in {ExecutionMode.SHADOW.value, ExecutionMode.SIMULATION.value}:
            raise ValueError("unsupported portfolio account mode")
        self.settings = settings
        self.policy = policy
        self.store = store
        self.execution_store = execution_store
        self.risk_store = risk_store
        self.maintenance_rate_provider = maintenance_rate_provider
        self.correlation_group_provider = correlation_group_provider or (lambda symbol: "default")
        self.health = PortfolioEngineHealth()

    def _record_failure(self, exc: Exception) -> None:
        self.health.failures += 1
        self.health.last_error = f"{exc.__class__.__name__}: {exc}"

    def initialize(
        self,
        *,
        initial_capital: Decimal | int | float | str,
        source_id: str,
        event_time: int,
    ) -> None:
        transaction = initialize_account_transaction(
            account_id=self.settings.account_id,
            initial_capital=initial_capital,
            event_time=event_time,
            source_id=source_id,
            policy=self.policy,
        )
        self.store.initialize_account(
            account_id=self.settings.account_id,
            policy=self.policy,
            transaction=transaction,
        )
        self.health.initialized = True
        self.health.last_error = None

    def _assert_initialized(self) -> None:
        instance = self.store.load_instance(self.settings.account_id)
        if instance is None:
            raise PortfolioHealthError("portfolio account has not been initialized")
        if instance.status is AccountStatus.HALTED:
            raise PortfolioHealthError(
                f"portfolio account is halted: {instance.halt_reason}"
            )
        self.health.initialized = True

    def _exact_command_source(
        self,
        stored: StoredExecutionCommand,
    ) -> StoredExecutionCommand:
        command = stored.command
        sequence = command.storage_sequence
        if sequence is None:
            raise PortfolioSourceError("execution command has no storage sequence")
        rows = self.execution_store.load_commands(
            after_sequence=sequence - 1,
            through_sequence=sequence,
            limit=1,
        )
        if len(rows) != 1:
            raise PortfolioSourceError(
                f"execution command storage sequence is missing: {sequence}"
            )
        exact = rows[0]
        if exact.command.semantic_dict() != command.semantic_dict():
            raise PortfolioSourceError(
                f"execution command semantics differ from storage: {command.command_id}"
            )
        if exact.delivery_state is not stored.delivery_state:
            raise PortfolioSourceError("execution command delivery state changed in transit")
        if exact.command.account_id != self.settings.account_id:
            raise PortfolioSourceError("execution command belongs to another account")
        if (
            self.settings.execution_engine_instance_ids is not None
            and exact.command.engine_instance_id
            not in self.settings.execution_engine_instance_ids
        ):
            raise PortfolioSourceError("execution command came from an unapproved engine instance")
        if exact.command.mode.value != self.policy.mode.value:
            raise PortfolioSourceError("execution command mode differs from account mode")
        return exact

    def _risk_source(self, command: ExecutionCommand) -> RiskDecision:
        sequence = command.source_decision_storage_sequence
        rows = self.risk_store.load_decisions(
            after_sequence=sequence - 1,
            through_sequence=sequence,
            limit=1,
        )
        if len(rows) != 1:
            raise PortfolioSourceError(
                f"risk decision storage sequence is missing: {sequence}"
            )
        stored = rows[0]
        decision = stored.decision
        mismatches = []
        if decision.decision_id != command.source_decision_id:
            mismatches.append("decision_id")
        if decision.storage_sequence != sequence:
            mismatches.append("storage_sequence")
        if decision.status is not RiskDecisionStatus.APPROVED:
            mismatches.append("status")
        if decision.account_id != command.account_id:
            mismatches.append("account_id")
        if decision.symbol != command.symbol:
            mismatches.append("symbol")
        if decision.approved_action is not command.action:
            mismatches.append("approved_action")
        if mismatches:
            raise PortfolioSourceError(
                "execution command risk source mismatch: " + ", ".join(mismatches)
            )
        return decision

    def _locked_exit_quantity(self, symbol: str) -> Decimal:
        return sum(
            (
                reservation.reserved_exit_quantity
                for reservation in self.store.active_reservations(
                    self.settings.account_id,
                    symbol=symbol,
                )
                if not reservation.entry
            ),
            ZERO,
        )

    def process_command(
        self,
        stored: StoredExecutionCommand,
        *,
        processed_at: int | None = None,
    ) -> CommandAccountingResult:
        current = int(time.time() * 1000) if processed_at is None else processed_at
        if current < 0:
            raise ValueError("processed_at must be non-negative")
        self._assert_initialized()
        self.health.commands_received += 1
        self.health.last_command_id = stored.command.command_id
        try:
            exact = self._exact_command_source(stored)
            command = exact.command
            sequence = int(command.storage_sequence or 0)
            existing = self.store.load_execution_command_event(
                self.settings.account_id,
                command.command_id,
            )
            if existing is not None:
                self.health.command_duplicates += 1
                self.health.last_error = None
                return CommandAccountingResult(
                    command.command_id,
                    sequence,
                    existing.disposition,
                    self.store.load_reservation(command.command_id),
                    True,
                )

            executable = command.status in {
                ExecutionCommandStatus.SHADOW_RECORDED,
                ExecutionCommandStatus.DISPATCH_PENDING,
                ExecutionCommandStatus.DISPATCHED,
                ExecutionCommandStatus.COMPLETED,
            }
            if not executable:
                disposition = self.store.record_execution_command_disposition(
                    account_id=self.settings.account_id,
                    source_sequence=sequence,
                    command=command,
                    disposition=StoredExecutionDisposition.NON_EXECUTABLE,
                    processed_at=current,
                )
                self.health.commands_processed += 1
                self.health.last_error = None
                return CommandAccountingResult(
                    command.command_id,
                    sequence,
                    disposition.disposition,
                    None,
                    disposition.duplicate,
                )

            decision = self._risk_source(command)
            position = self.store.load_position(
                self.settings.account_id,
                command.symbol,
            )
            plan = plan_command_reservation(
                account_id=self.settings.account_id,
                command=command,
                decision=decision,
                balances=self.store.balances(self.settings.account_id),
                policy=self.policy,
                now_ms=current,
                current_position=position,
                already_locked_exit_quantity=self._locked_exit_quantity(command.symbol),
            )
            commit = self.store.commit_execution_command_reservation(
                plan,
                policy=self.policy,
                source_sequence=sequence,
                command=command,
                processed_at=current,
            )
            if commit.duplicate:
                self.health.command_duplicates += 1
            else:
                self.health.commands_processed += 1
            self.health.last_error = None
            return CommandAccountingResult(
                command.command_id,
                sequence,
                StoredExecutionDisposition.RESERVED.value,
                commit.reservation,
                commit.duplicate,
            )
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _exact_update_source(self, stored: StoredOrderUpdate) -> StoredOrderUpdate:
        rows = self.execution_store.load_order_updates(
            after_sequence=stored.storage_sequence - 1,
            through_sequence=stored.storage_sequence,
            limit=1,
        )
        if len(rows) != 1:
            raise PortfolioSourceError(
                f"order update storage sequence is missing: {stored.storage_sequence}"
            )
        exact = rows[0]
        if exact.update.to_dict() != stored.update.to_dict():
            raise PortfolioSourceError(
                f"order update semantics differ from storage: {stored.update.update_id}"
            )
        if exact.prior_state is not stored.prior_state or exact.new_state is not stored.new_state:
            raise PortfolioSourceError("order update transition changed in transit")
        return exact

    @staticmethod
    def _order_for_update(
        command: ExecutionCommand,
        update: StoredOrderUpdate,
    ) -> ExecutionOrder:
        matches = [
            order for order in command.orders if order.order_id == update.update.order_id
        ]
        if len(matches) != 1:
            raise PortfolioSourceError("order update has no unique immutable order plan")
        return matches[0]

    def process_update(
        self,
        stored: StoredOrderUpdate,
        *,
        processed_at: int | None = None,
    ) -> UpdateAccountingResult:
        current = int(time.time() * 1000) if processed_at is None else processed_at
        if current < 0:
            raise ValueError("processed_at must be non-negative")
        self._assert_initialized()
        self.health.updates_received += 1
        self.health.last_update_id = stored.update.update_id
        try:
            exact = self._exact_update_source(stored)
            existing = self.store.load_execution_update_event(
                self.settings.account_id,
                exact.update.update_id,
            )
            if existing is not None:
                self.health.update_duplicates += 1
                self.health.last_error = None
                return UpdateAccountingResult(
                    exact.update.update_id,
                    exact.storage_sequence,
                    existing.disposition,
                    None,
                    True,
                )

            command_event = self.store.load_execution_command_event_by_id(
                self.settings.account_id,
                exact.update.order_id,
            )
            command_id = self.execution_store.command_id_for_order(
                exact.update.order_id
            )
            command_event = self.store.load_execution_command_event(
                self.settings.account_id,
                command_id,
            )
            if command_event is None:
                self.health.barriers += 1
                raise PortfolioBarrierError(
                    f"order update overtook command reservation: {command_id}"
                )
            stored_command = self.execution_store.load_command(command_id)
            if stored_command is None:
                raise PortfolioSourceError(
                    f"execution command disappeared for update: {command_id}"
                )
            command = stored_command.command
            order = self._order_for_update(command, exact)
            reservation = self.store.load_reservation(command_id)
            if reservation is None:
                if command_event.disposition == StoredExecutionDisposition.NON_EXECUTABLE.value:
                    disposition = self.store.record_execution_update_disposition(
                        account_id=self.settings.account_id,
                        source_sequence=exact.storage_sequence,
                        stored_update=exact,
                        command_id=command_id,
                        disposition=StoredExecutionDisposition.NO_ACCOUNTING,
                        processed_at=current,
                    )
                    self.health.no_accounting_updates += 1
                    return UpdateAccountingResult(
                        exact.update.update_id,
                        exact.storage_sequence,
                        disposition.disposition,
                        None,
                        disposition.duplicate,
                    )
                raise PortfolioBarrierError(
                    f"execution command has no account reservation: {command_id}"
                )

            cursor = self.store.load_order_cursor(order.order_id, order.command_id)
            cumulative_quantity = as_decimal(
                exact.update.cumulative_filled_quantity,
                "cumulative_filled_quantity",
            )
            cumulative_quote = self.policy.quantize_money(
                exact.update.cumulative_quote_value
            )
            cumulative_fee = self.policy.quantize_money(exact.update.cumulative_fee)
            if (
                cumulative_quantity == cursor.cumulative_filled_quantity
                and cumulative_quote == cursor.cumulative_quote_value
                and cumulative_fee == cursor.cumulative_fee
            ):
                disposition = self.store.record_execution_update_disposition(
                    account_id=self.settings.account_id,
                    source_sequence=exact.storage_sequence,
                    stored_update=exact,
                    command_id=command_id,
                    disposition=StoredExecutionDisposition.NO_ACCOUNTING,
                    processed_at=current,
                )
                self.health.no_accounting_updates += 1
                self.health.updates_processed += 1
                self.health.last_error = None
                return UpdateAccountingResult(
                    exact.update.update_id,
                    exact.storage_sequence,
                    disposition.disposition,
                    None,
                    disposition.duplicate,
                )

            balances = self.store.balances(self.settings.account_id)
            position = self.store.load_position(
                self.settings.account_id,
                command.symbol,
            )
            if order.reduce_only:
                if position is None:
                    raise PortfolioAccountingError(
                        "reduce-only execution update has no open position"
                    )
                result = apply_close_fill(
                    command=command,
                    order=order,
                    update=exact.update,
                    cursor=cursor,
                    reservation=reservation,
                    balances=balances,
                    policy=self.policy,
                    position=position,
                )
            else:
                decision = self._risk_source(command)
                notional_after = (
                    (position.notional if position is not None else ZERO)
                    + cumulative_quote
                    - cursor.cumulative_quote_value
                )
                maintenance_rate = as_decimal(
                    self.maintenance_rate_provider(command.symbol, notional_after),
                    "maintenance_margin_rate",
                )
                result = apply_open_fill(
                    command=command,
                    decision=decision,
                    order=order,
                    update=exact.update,
                    cursor=cursor,
                    reservation=reservation,
                    balances=balances,
                    policy=self.policy,
                    maintenance_margin_rate=maintenance_rate,
                    correlation_group=self.correlation_group_provider(command.symbol),
                    position=position,
                )
            commit = self.store.commit_execution_fill(
                result,
                policy=self.policy,
                source_sequence=exact.storage_sequence,
                stored_update=exact,
                processed_at=current,
            )
            if commit.duplicate:
                self.health.update_duplicates += 1
            else:
                self.health.updates_processed += 1
                self.health.fills_accounted += 1
            self.health.last_error = None
            return UpdateAccountingResult(
                exact.update.update_id,
                exact.storage_sequence,
                StoredExecutionDisposition.ACCOUNTED.value,
                commit,
                commit.duplicate,
            )
        except Exception as exc:
            self._record_failure(exc)
            raise

    def release_terminal_reservations(self, *, now_ms: int | None = None) -> int:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        released = 0
        for reservation in self.store.active_reservations(self.settings.account_id):
            command = self.execution_store.load_command(reservation.command_id)
            if command is None:
                self._critical_finding(
                    kind=ReconciliationKind.EXECUTION_COMMAND,
                    source_id=reservation.command_id,
                    found_at=current,
                    expected={"command": "present"},
                    actual={"command": "missing"},
                )
                continue
            if command.delivery_state not in {
                ExecutionDeliveryState.RECORDED,
                ExecutionDeliveryState.COMPLETED,
                ExecutionDeliveryState.EXPIRED,
                ExecutionDeliveryState.FAILED,
            }:
                continue
            release = release_command_reservation(
                reservation=reservation,
                policy=self.policy,
                event_time=max(current, reservation.updated_at),
                reason=f"execution_{command.delivery_state.value}",
            )
            commit = self.store.release_reservation(release, self.policy)
            if not commit.duplicate:
                released += 1
                self.health.reservations_released += 1
        return released

    def apply_mark(
        self,
        *,
        source_event_id: str,
        symbol: str,
        observed_at: int,
        source_sequence: int,
        mark_price: Decimal | int | float | str,
    ) -> MarkCommit:
        self._assert_initialized()
        fact = make_mark_price_fact(
            account_id=self.settings.account_id,
            source_event_id=source_event_id,
            symbol=symbol,
            observed_at=observed_at,
            source_sequence=source_sequence,
            mark_price=mark_price,
        )
        result = apply_mark_fact(
            self.store.load_position(self.settings.account_id, symbol),
            fact,
        )
        commit = self.store.commit_mark(result, self.policy)
        if not commit.duplicate:
            self.health.marks_applied += 1
        self.health.last_error = None
        return commit

    def apply_funding(
        self,
        *,
        source_id: str,
        symbol: str,
        event_time: int,
        rate: Decimal | int | float | str,
        mark_price: Decimal | int | float | str,
    ) -> Any:
        self._assert_initialized()
        position = self.store.load_position(self.settings.account_id, symbol)
        if position is None:
            return None
        result = apply_funding_to_position(
            account_id=self.settings.account_id,
            position=position,
            rate=rate,
            mark_price=mark_price,
            event_time=event_time,
            source_id=source_id,
            policy=self.policy,
        )
        commit = self.store.commit_funding(
            account_id=self.settings.account_id,
            symbol=symbol,
            source_id=source_id,
            result=result,
            policy=self.policy,
        )
        if not commit.duplicate:
            self.health.funding_applied += 1
        self.health.last_error = None
        return commit

    def _critical_finding(
        self,
        *,
        kind: ReconciliationKind,
        source_id: str,
        found_at: int,
        expected: dict[str, Any],
        actual: dict[str, Any],
        evidence: dict[str, Any] | None = None,
    ) -> None:
        finding = make_reconciliation_finding(
            account_id=self.settings.account_id,
            kind=kind,
            severity=ReconciliationSeverity.CRITICAL,
            source_id=source_id,
            found_at=found_at,
            expected=expected,
            actual=actual,
            evidence=evidence,
        )
        commit = self.store.commit_finding(finding)
        if not commit.duplicate:
            self.health.reconciliation_failures += 1

    def reconcile_execution(self, *, now_ms: int | None = None) -> int:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        findings = 0
        for reservation in self.store.active_reservations(self.settings.account_id):
            stored_command = self.execution_store.load_command(reservation.command_id)
            if stored_command is None:
                self._critical_finding(
                    kind=ReconciliationKind.EXECUTION_COMMAND,
                    source_id=reservation.command_id,
                    found_at=current,
                    expected={"present": True},
                    actual={"present": False},
                )
                findings += 1
                continue
            for order in stored_command.orders:
                cursor = self.store.load_order_cursor(order.order_id, order.command_id)
                if (
                    as_decimal(order.cumulative_filled_quantity, "execution fill")
                    != cursor.cumulative_filled_quantity
                    or self.policy.quantize_money(order.cumulative_quote_value)
                    != cursor.cumulative_quote_value
                    or self.policy.quantize_money(order.cumulative_fee)
                    != cursor.cumulative_fee
                ):
                    self._critical_finding(
                        kind=ReconciliationKind.EXECUTION_FILL,
                        source_id=order.order_id,
                        found_at=current,
                        expected={
                            "filled": str(order.cumulative_filled_quantity),
                            "quote": str(order.cumulative_quote_value),
                            "fee": str(order.cumulative_fee),
                        },
                        actual={
                            "filled": str(cursor.cumulative_filled_quantity),
                            "quote": str(cursor.cumulative_quote_value),
                            "fee": str(cursor.cumulative_fee),
                        },
                        evidence={"command_id": reservation.command_id},
                    )
                    findings += 1
        return findings

    @staticmethod
    def _utc_day(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).date().isoformat()

    def snapshot(self, *, observed_at: int | None = None) -> AccountSnapshot:
        current = int(time.time() * 1000) if observed_at is None else observed_at
        if current < 0:
            raise ValueError("observed_at must be non-negative")
        instance = self.store.load_instance(self.settings.account_id)
        if instance is None:
            raise PortfolioHealthError("portfolio account has not been initialized")
        positions = tuple(self.store.list_positions(self.settings.account_id))
        halt_reasons = []
        for position in positions:
            mark = self.store.latest_mark(self.settings.account_id, position.symbol)
            if mark is None:
                halt_reasons.append(AccountHaltReason.STALE_MARK)
                continue
            if mark.observed_at > current:
                halt_reasons.append(AccountHaltReason.STALE_MARK)
            elif current - mark.observed_at > self.policy.max_mark_age_ms:
                halt_reasons.append(AccountHaltReason.STALE_MARK)
        prior = self.store.latest_snapshot(self.settings.account_id)
        balances = self.store.balances(self.settings.account_id)
        provisional = build_account_snapshot(
            account_id=self.settings.account_id,
            observed_at=current,
            balances=balances,
            positions=positions,
            day_start_equity=(
                balances.settled_assets
                if prior is None
                else (
                    prior.day_start_equity
                    if self._utc_day(prior.observed_at) == self._utc_day(current)
                    else prior.equity
                )
            ),
            peak_equity=(balances.settled_assets if prior is None else prior.peak_equity),
            consecutive_losses=self.store.consecutive_losses(self.settings.account_id),
            watermarks=AccountWatermarks(
                execution_command_sequence=self.store.latest_execution_command_sequence(
                    self.settings.account_id
                ),
                execution_update_sequence=self.store.latest_execution_update_sequence(
                    self.settings.account_id
                ),
                market_sequence=self.store.latest_market_sequence(
                    self.settings.account_id
                ),
                funding_sequence=self.store.latest_funding_sequence(
                    self.settings.account_id
                ),
            ),
            global_halt=(instance.status is AccountStatus.HALTED or bool(halt_reasons)),
            halt_reasons=tuple(dict.fromkeys(halt_reasons)),
        )
        snapshot = build_account_snapshot(
            account_id=provisional.account_id,
            observed_at=provisional.observed_at,
            balances=provisional.balances,
            positions=provisional.positions,
            day_start_equity=provisional.day_start_equity,
            peak_equity=max(provisional.peak_equity, provisional.equity),
            consecutive_losses=provisional.consecutive_losses,
            watermarks=provisional.watermarks,
            global_halt=provisional.global_halt,
            day_halt=provisional.day_halt,
            drawdown_halt=provisional.drawdown_halt,
            halt_reasons=provisional.halt_reasons,
        )
        commit = self.store.commit_snapshot(snapshot)
        self.health.snapshots_created += 0 if commit.duplicate else 1
        self.health.last_snapshot_id = commit.snapshot.snapshot_id
        self.health.last_error = None
        return commit.snapshot

    def bootstrap(self, *, now_ms: int | None = None) -> PortfolioBootstrapResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        self._assert_initialized()
        commands_processed = 0
        updates_processed = 0
        command_after = self.store.latest_execution_command_sequence(
            self.settings.account_id
        )
        while True:
            commands = self.execution_store.load_commands(
                after_sequence=command_after,
                account_id=self.settings.account_id,
                limit=self.settings.command_batch_size,
            )
            if not commands:
                break
            result = self.process_command(commands[0], processed_at=current)
            command_after = result.source_sequence
            commands_processed += 0 if result.duplicate else 1

        update_after = self.store.latest_execution_update_sequence(
            self.settings.account_id
        )
        while True:
            updates = self.execution_store.load_order_updates(
                after_sequence=update_after,
                account_id=self.settings.account_id,
                limit=self.settings.update_batch_size,
            )
            if not updates:
                break
            result = self.process_update(updates[0], processed_at=current)
            update_after = result.source_sequence
            updates_processed += 0 if result.duplicate else 1

        released = self.release_terminal_reservations(now_ms=current)
        self.reconcile_execution(now_ms=current)
        return PortfolioBootstrapResult(
            commands_processed=commands_processed,
            updates_processed=updates_processed,
            reservations_released=released,
            command_high_watermark=command_after,
            update_high_watermark=update_after,
        )

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "engine": self.health.snapshot(),
            "store": self.store.metrics(self.settings.account_id),
            "network_write_enabled": False,
            "account_id": self.settings.account_id,
            "mode": self.policy.mode.value,
        }
