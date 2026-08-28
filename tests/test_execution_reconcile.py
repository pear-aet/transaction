from dataclasses import replace

import pytest

from crypto_quant.execution_contracts import (
    ExecutionOrder,
    ExecutionOrderType,
    ExecutionSide,
    OrderLegType,
    OrderState,
)
from crypto_quant.execution_reconcile import (
    ExecutionReconciliationError,
    ObservedExecutionOrder,
    ReconciliationIssueKind,
    reconcile_execution_orders,
    require_complete_reconciliation,
)

COMMAND_ID = "execution-command-1"
NOW = 900_001


def local_order(
    *,
    state: OrderState = OrderState.ACKNOWLEDGED,
    filled: float = 0,
    average: float | None = None,
) -> ExecutionOrder:
    broker_order_id = None if state is OrderState.PLANNED else "sim-order-1"
    return ExecutionOrder(
        order_id="execution-order-1",
        command_id=COMMAND_ID,
        client_order_id="cq_primary_1",
        leg_type=OrderLegType.PRIMARY,
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.MARKET,
        quantity=10,
        reduce_only=False,
        state=state,
        broker_order_id=broker_order_id,
        cumulative_filled_quantity=filled,
        cumulative_quote_value=0 if average is None else filled * average,
        cumulative_fee=0 if filled == 0 else 0.1,
        average_fill_price=average,
        created_at=900_000,
        updated_at=900_000,
        evidence={"submitted": False, "network_write": False},
    )


def observed(
    *,
    state: OrderState = OrderState.ACKNOWLEDGED,
    filled: float = 0,
    average: float | None = None,
    client_order_id: str = "cq_primary_1",
    broker_order_id: str = "sim-order-1",
) -> ObservedExecutionOrder:
    return ObservedExecutionOrder(
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
        state=state,
        cumulative_filled_quantity=filled,
        cumulative_quote_value=0 if average is None else filled * average,
        cumulative_fee=0 if filled == 0 else 0.1,
        observed_at=NOW,
        average_fill_price=average,
        evidence={"submitted": False, "network_write": False},
    )


def test_exact_complete_ledger_reconciliation_is_clean() -> None:
    order = local_order()
    report = reconcile_execution_orders(
        COMMAND_ID,
        [order],
        [observed()],
        observed_at=NOW,
    )

    assert report.complete is True
    assert report.matched_order_ids == (order.order_id,)
    assert report.updates == ()
    assert report.issues == ()
    assert require_complete_reconciliation(report) is report


def test_forward_partial_fill_is_returned_as_a_valid_deterministic_update() -> None:
    report = reconcile_execution_orders(
        COMMAND_ID,
        [local_order()],
        [observed(state=OrderState.PARTIALLY_FILLED, filled=4, average=100)],
        observed_at=NOW,
    )
    duplicate = reconcile_execution_orders(
        COMMAND_ID,
        [local_order()],
        [observed(state=OrderState.PARTIALLY_FILLED, filled=4, average=100)],
        observed_at=NOW,
    )

    assert report.complete is True
    assert len(report.updates) == 1
    assert report.updates == duplicate.updates
    update = report.updates[0]
    assert update.new_state is OrderState.PARTIALLY_FILLED
    assert update.cumulative_filled_quantity == 4
    assert update.evidence["submitted"] is False
    assert update.evidence["network_write"] is False


def test_missing_unknown_and_unexpected_activation_fail_closed() -> None:
    missing = reconcile_execution_orders(
        COMMAND_ID,
        [local_order()],
        [],
        observed_at=NOW,
    )
    assert [issue.kind for issue in missing.issues] == [
        ReconciliationIssueKind.MISSING_OBSERVED
    ]

    unknown = reconcile_execution_orders(
        COMMAND_ID,
        [],
        [observed(client_order_id="unknown_1")],
        observed_at=NOW,
    )
    assert [issue.kind for issue in unknown.issues] == [
        ReconciliationIssueKind.UNKNOWN_OBSERVED
    ]

    planned = reconcile_execution_orders(
        COMMAND_ID,
        [local_order(state=OrderState.PLANNED)],
        [observed()],
        observed_at=NOW,
    )
    assert [issue.kind for issue in planned.issues] == [
        ReconciliationIssueKind.UNEXPECTED_ACTIVATION
    ]

    with pytest.raises(ExecutionReconciliationError, match="missing_observed"):
        require_complete_reconciliation(missing)


def test_identifier_and_state_regressions_are_not_auto_repaired() -> None:
    identifier = reconcile_execution_orders(
        COMMAND_ID,
        [local_order()],
        [observed(broker_order_id="another-broker-order")],
        observed_at=NOW,
    )
    assert [issue.kind for issue in identifier.issues] == [
        ReconciliationIssueKind.BROKER_ID_MISMATCH
    ]

    partial = local_order(state=OrderState.PARTIALLY_FILLED, filled=4, average=100)
    regression = reconcile_execution_orders(
        COMMAND_ID,
        [replace(partial, updated_at=900_000)],
        [observed(state=OrderState.ACKNOWLEDGED)],
        observed_at=NOW,
    )
    assert [issue.kind for issue in regression.issues] == [
        ReconciliationIssueKind.INVALID_TRANSITION
    ]
    assert "invalid order transition" in regression.issues[0].detail


def test_observed_snapshot_requires_local_simulation_safety_flags() -> None:
    with pytest.raises(ValueError, match="submitted=false"):
        ObservedExecutionOrder(
            "cq_primary_1",
            "sim-order-1",
            OrderState.ACKNOWLEDGED,
            0,
            0,
            0,
            NOW,
            evidence={"submitted": True, "network_write": False},
        )
    with pytest.raises(ValueError, match="network writes"):
        ObservedExecutionOrder(
            "cq_primary_1",
            "sim-order-1",
            OrderState.ACKNOWLEDGED,
            0,
            0,
            0,
            NOW,
            evidence={"submitted": False, "network_write": True},
        )
