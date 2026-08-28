from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .domain import finite_json
from .execution import OrderTransitionError, apply_order_update
from .execution_contracts import ExecutionOrder, OrderState, OrderUpdate


class ReconciliationIssueKind(StrEnum):
    MISSING_OBSERVED = "missing_observed"
    UNKNOWN_OBSERVED = "unknown_observed"
    UNEXPECTED_ACTIVATION = "unexpected_activation"
    BROKER_ID_MISMATCH = "broker_id_mismatch"
    INVALID_TRANSITION = "invalid_transition"


@dataclass(frozen=True, slots=True)
class ObservedExecutionOrder:
    client_order_id: str
    broker_order_id: str
    state: OrderState
    cumulative_filled_quantity: float
    cumulative_quote_value: float
    cumulative_fee: float
    observed_at: int
    average_fill_price: float | None = None
    evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.broker_order_id:
            raise ValueError("observed execution order identifiers must not be empty")
        if self.observed_at < 0:
            raise ValueError("observed execution order time must be non-negative")
        numeric = (
            self.cumulative_filled_quantity,
            self.cumulative_quote_value,
            self.cumulative_fee,
        )
        if any(not math.isfinite(value) or value < 0 for value in numeric):
            raise ValueError("observed execution amounts must be finite and non-negative")
        if self.cumulative_filled_quantity > 0:
            if self.average_fill_price is None or self.average_fill_price <= 0:
                raise ValueError("observed fill requires an average fill price")
            expected_quote = self.cumulative_filled_quantity * self.average_fill_price
            if not math.isclose(
                self.cumulative_quote_value,
                expected_quote,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                raise ValueError("observed quote value is inconsistent with average fill price")
        elif self.average_fill_price is not None or self.cumulative_quote_value != 0:
            raise ValueError("unfilled observed order cannot contain fill pricing")
        selected_evidence = self.evidence or {}
        json.dumps(selected_evidence, sort_keys=True, allow_nan=False)
        if selected_evidence.get("submitted") is not False:
            raise ValueError("observed simulation order must remain submitted=false")
        if selected_evidence.get("network_write") is not False:
            raise ValueError("observed simulation order cannot permit network writes")

    def to_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                "client_order_id": self.client_order_id,
                "broker_order_id": self.broker_order_id,
                "state": self.state.value,
                "cumulative_filled_quantity": self.cumulative_filled_quantity,
                "cumulative_quote_value": self.cumulative_quote_value,
                "cumulative_fee": self.cumulative_fee,
                "observed_at": self.observed_at,
                "average_fill_price": self.average_fill_price,
                "evidence": self.evidence or {},
            }
        )


@dataclass(frozen=True, slots=True)
class ReconciliationIssue:
    kind: ReconciliationIssueKind
    client_order_id: str
    detail: str

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.detail:
            raise ValueError("reconciliation issue fields must not be empty")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "client_order_id": self.client_order_id,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ExecutionReconciliationReport:
    command_id: str
    observed_at: int
    matched_order_ids: tuple[str, ...]
    updates: tuple[OrderUpdate, ...]
    issues: tuple[ReconciliationIssue, ...]

    def __post_init__(self) -> None:
        if not self.command_id:
            raise ValueError("reconciliation command_id must not be empty")
        if self.observed_at < 0:
            raise ValueError("reconciliation observed_at must be non-negative")
        if len(self.matched_order_ids) != len(set(self.matched_order_ids)):
            raise ValueError("reconciliation matched orders must be unique")
        update_ids = [update.update_id for update in self.updates]
        if len(update_ids) != len(set(update_ids)):
            raise ValueError("reconciliation updates must be unique")

    @property
    def complete(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                "command_id": self.command_id,
                "observed_at": self.observed_at,
                "complete": self.complete,
                "matched_order_ids": list(self.matched_order_ids),
                "updates": [update.to_dict() for update in self.updates],
                "issues": [issue.to_dict() for issue in self.issues],
            }
        )


class ExecutionReconciliationError(RuntimeError):
    """Raised when a complete broker ledger differs from persisted execution state."""

    def __init__(self, report: ExecutionReconciliationReport) -> None:
        self.report = report
        kinds = ", ".join(issue.kind.value for issue in report.issues)
        super().__init__(f"execution reconciliation failed: {kinds}")


def _update_id(
    command_id: str,
    order: ExecutionOrder,
    observed: ObservedExecutionOrder,
) -> str:
    canonical = json.dumps(
        {
            "command_id": command_id,
            "order_id": order.order_id,
            "observed": observed.to_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.blake2s(canonical.encode(), digest_size=16).hexdigest()
    return f"reconciliation_update:{digest}"


def _same_runtime_state(
    local: ExecutionOrder,
    observed: ObservedExecutionOrder,
) -> bool:
    return (
        local.state is observed.state
        and local.broker_order_id == observed.broker_order_id
        and math.isclose(
            local.cumulative_filled_quantity,
            observed.cumulative_filled_quantity,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        and math.isclose(
            local.cumulative_quote_value,
            observed.cumulative_quote_value,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        and math.isclose(
            local.cumulative_fee,
            observed.cumulative_fee,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        and local.average_fill_price == observed.average_fill_price
    )


def reconcile_execution_orders(
    command_id: str,
    local_orders: Iterable[ExecutionOrder],
    observed_orders: Iterable[ObservedExecutionOrder],
    *,
    observed_at: int,
) -> ExecutionReconciliationReport:
    """Compare against a complete broker order ledger; never silently repairs divergence."""
    if not command_id or observed_at < 0:
        raise ValueError("invalid reconciliation identity or time")
    local = tuple(local_orders)
    observed = tuple(observed_orders)
    if any(order.command_id != command_id for order in local):
        raise ValueError("reconciliation contains an order from another command")
    local_ids = [order.client_order_id for order in local]
    observed_ids = [order.client_order_id for order in observed]
    if len(local_ids) != len(set(local_ids)):
        raise ValueError("local reconciliation client identifiers must be unique")
    if len(observed_ids) != len(set(observed_ids)):
        raise ValueError("observed reconciliation client identifiers must be unique")
    if any(item.observed_at != observed_at for item in observed):
        raise ValueError("observed reconciliation snapshot times must match")

    local_by_client = {order.client_order_id: order for order in local}
    observed_by_client = {item.client_order_id: item for item in observed}
    matched: list[str] = []
    updates: list[OrderUpdate] = []
    issues: list[ReconciliationIssue] = []

    for client_order_id, order in local_by_client.items():
        item = observed_by_client.get(client_order_id)
        if item is None:
            if order.state is not OrderState.PLANNED:
                issues.append(
                    ReconciliationIssue(
                        ReconciliationIssueKind.MISSING_OBSERVED,
                        client_order_id,
                        (
                            f"persisted {order.state.value} order is absent from "
                            "complete broker ledger"
                        ),
                    )
                )
            continue
        if order.state is OrderState.PLANNED:
            issues.append(
                ReconciliationIssue(
                    ReconciliationIssueKind.UNEXPECTED_ACTIVATION,
                    client_order_id,
                    "broker contains an order that remains planned locally",
                )
            )
            continue
        if order.broker_order_id not in {None, item.broker_order_id}:
            issues.append(
                ReconciliationIssue(
                    ReconciliationIssueKind.BROKER_ID_MISMATCH,
                    client_order_id,
                    "broker order identifier differs from persisted identity",
                )
            )
            continue
        if _same_runtime_state(order, item):
            matched.append(order.order_id)
            continue
        update = OrderUpdate(
            update_id=_update_id(command_id, order, item),
            order_id=order.order_id,
            event_time=observed_at,
            new_state=item.state,
            cumulative_filled_quantity=item.cumulative_filled_quantity,
            cumulative_quote_value=item.cumulative_quote_value,
            cumulative_fee=item.cumulative_fee,
            average_fill_price=item.average_fill_price,
            broker_order_id=item.broker_order_id,
            evidence={
                "source": "complete_broker_ledger_reconciliation",
                "submitted": False,
                "network_write": False,
                "observed_evidence": item.evidence or {},
            },
        )
        try:
            apply_order_update(order, update)
        except OrderTransitionError as exc:
            issues.append(
                ReconciliationIssue(
                    ReconciliationIssueKind.INVALID_TRANSITION,
                    client_order_id,
                    str(exc),
                )
            )
        else:
            updates.append(update)

    for client_order_id in sorted(set(observed_by_client) - set(local_by_client)):
        issues.append(
            ReconciliationIssue(
                ReconciliationIssueKind.UNKNOWN_OBSERVED,
                client_order_id,
                "broker ledger contains an order unknown to the execution command",
            )
        )

    return ExecutionReconciliationReport(
        command_id=command_id,
        observed_at=observed_at,
        matched_order_ids=tuple(sorted(matched)),
        updates=tuple(updates),
        issues=tuple(issues),
    )


def require_complete_reconciliation(
    report: ExecutionReconciliationReport,
) -> ExecutionReconciliationReport:
    if not report.complete:
        raise ExecutionReconciliationError(report)
    return report
