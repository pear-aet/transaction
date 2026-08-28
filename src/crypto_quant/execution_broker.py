from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Protocol

from .domain import finite_json
from .execution_contracts import (
    ExecutionCommand,
    ExecutionCommandStatus,
    ExecutionMode,
    ExecutionOrder,
    OrderLegType,
    OrderState,
    OrderUpdate,
)


class ExecutionBrokerError(RuntimeError):
    """Raised when a local execution adapter cannot accept an operation."""


class ExecutionBroker(Protocol):
    def dispatch(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...],
        now_ms: int,
    ) -> tuple[OrderUpdate, ...]: ...

    def activate_protection(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...],
        now_ms: int,
    ) -> tuple[OrderUpdate, ...]: ...

    def cancel(
        self,
        order: ExecutionOrder,
        *,
        event_time: int,
        reason: str,
    ) -> tuple[OrderUpdate, ...]: ...


class ShadowBroker:
    """Explicit non-broker used to prove shadow mode has no dispatch path."""

    def dispatch(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...],
        now_ms: int,
    ) -> tuple[OrderUpdate, ...]:
        del command, orders, now_ms
        raise ExecutionBrokerError("shadow execution has no dispatch capability")

    def activate_protection(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...],
        now_ms: int,
    ) -> tuple[OrderUpdate, ...]:
        del command, orders, now_ms
        raise ExecutionBrokerError("shadow execution has no protection dispatch capability")

    def cancel(
        self,
        order: ExecutionOrder,
        *,
        event_time: int,
        reason: str,
    ) -> tuple[OrderUpdate, ...]:
        del order, event_time, reason
        raise ExecutionBrokerError("shadow execution has no cancellation capability")


class SimulatedBroker:
    """Deterministic in-process broker with no network or exchange credentials."""

    @staticmethod
    def _identifier(prefix: str, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            finite_json(payload),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.blake2s(canonical.encode(), digest_size=16).hexdigest()
        return f"{prefix}:{digest}"

    @classmethod
    def _broker_order_id(cls, order: ExecutionOrder) -> str:
        return cls._identifier(
            "sim_order",
            {
                "order_id": order.order_id,
                "client_order_id": order.client_order_id,
            },
        )

    @classmethod
    def _update(
        cls,
        order: ExecutionOrder,
        *,
        new_state: OrderState,
        event_time: int,
        broker_order_id: str | None,
        cumulative_filled_quantity: float | None = None,
        cumulative_quote_value: float | None = None,
        cumulative_fee: float | None = None,
        average_fill_price: float | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> OrderUpdate:
        filled = (
            order.cumulative_filled_quantity
            if cumulative_filled_quantity is None
            else cumulative_filled_quantity
        )
        quote = (
            order.cumulative_quote_value
            if cumulative_quote_value is None
            else cumulative_quote_value
        )
        fee = order.cumulative_fee if cumulative_fee is None else cumulative_fee
        average = order.average_fill_price if average_fill_price is None else average_fill_price
        payload = {
            "order_id": order.order_id,
            "event_time": event_time,
            "new_state": new_state.value,
            "filled": format(filled, ".17g"),
            "quote": format(quote, ".17g"),
            "fee": format(fee, ".17g"),
            "average": None if average is None else format(average, ".17g"),
            "broker_order_id": broker_order_id,
            "evidence": evidence or {},
        }
        return OrderUpdate(
            update_id=cls._identifier("sim_update", payload),
            order_id=order.order_id,
            event_time=event_time,
            new_state=new_state,
            cumulative_filled_quantity=filled,
            cumulative_quote_value=quote,
            cumulative_fee=fee,
            average_fill_price=average,
            broker_order_id=broker_order_id,
            evidence={
                "broker": "simulated",
                "submitted": False,
                "network_write": False,
                **(evidence or {}),
            },
        )

    @staticmethod
    def _primary(orders: tuple[ExecutionOrder, ...]) -> ExecutionOrder:
        primary = [order for order in orders if order.leg_type is OrderLegType.PRIMARY]
        if len(primary) != 1:
            raise ExecutionBrokerError("simulation command requires exactly one primary order")
        return primary[0]

    @staticmethod
    def _protective(orders: tuple[ExecutionOrder, ...]) -> ExecutionOrder | None:
        protective = [order for order in orders if order.leg_type is OrderLegType.PROTECTIVE_STOP]
        if len(protective) > 1:
            raise ExecutionBrokerError("simulation command has multiple protective stops")
        return None if not protective else protective[0]

    @staticmethod
    def _assert_dispatchable(command: ExecutionCommand, now_ms: int) -> None:
        if command.mode is not ExecutionMode.SIMULATION:
            raise ExecutionBrokerError("simulated broker accepts simulation commands only")
        if command.status is not ExecutionCommandStatus.DISPATCH_PENDING:
            raise ExecutionBrokerError("simulated broker accepts dispatch-pending plans only")
        if command.storage_sequence is None:
            raise ExecutionBrokerError("execution command must be persisted before dispatch")
        if now_ms < command.created_at:
            raise ExecutionBrokerError("simulation dispatch cannot precede command creation")
        if now_ms > command.valid_until:
            raise ExecutionBrokerError("expired simulation command cannot be dispatched")
        if command.evidence.get("network_write") is not False:
            raise ExecutionBrokerError("simulation command attempted to enable network writes")

    def _dispatch_order(
        self,
        order: ExecutionOrder,
        now_ms: int,
        *,
        activation: str,
    ) -> tuple[OrderUpdate, ...]:
        if order.state is not OrderState.PLANNED:
            return ()
        broker_order_id = self._broker_order_id(order)
        dispatched = self._update(
            order,
            new_state=OrderState.DISPATCHED,
            event_time=now_ms,
            broker_order_id=broker_order_id,
            evidence={"operation": "dispatch", "activation": activation},
        )
        acknowledged = self._update(
            order,
            new_state=OrderState.ACKNOWLEDGED,
            event_time=now_ms,
            broker_order_id=broker_order_id,
            evidence={"operation": "acknowledge", "activation": activation},
        )
        return dispatched, acknowledged

    def dispatch(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...],
        now_ms: int,
    ) -> tuple[OrderUpdate, ...]:
        self._assert_dispatchable(command, now_ms)
        primary = self._primary(orders)
        if primary.terminal:
            raise ExecutionBrokerError("terminal primary order cannot be dispatched")
        return self._dispatch_order(primary, now_ms, activation="command")

    def activate_protection(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...],
        now_ms: int,
    ) -> tuple[OrderUpdate, ...]:
        if command.mode is not ExecutionMode.SIMULATION:
            raise ExecutionBrokerError("protective activation requires simulation mode")
        primary = self._primary(orders)
        protective = self._protective(orders)
        if protective is None:
            return ()
        if protective.activation_order_id != primary.order_id:
            raise ExecutionBrokerError("protective stop activation parent is invalid")
        if primary.state not in {OrderState.PARTIALLY_FILLED, OrderState.FILLED}:
            return ()
        return self._dispatch_order(protective, now_ms, activation=primary.state.value)

    def fill(
        self,
        order: ExecutionOrder,
        *,
        fill_quantity: float,
        fill_price: float,
        fee: float,
        event_time: int,
    ) -> OrderUpdate:
        if order.state not in {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.CANCEL_PENDING,
        }:
            raise ExecutionBrokerError(f"order cannot fill from {order.state.value}")
        if not math.isfinite(fill_quantity) or fill_quantity <= 0:
            raise ValueError("fill_quantity must be finite and positive")
        if not math.isfinite(fill_price) or fill_price <= 0:
            raise ValueError("fill_price must be finite and positive")
        if not math.isfinite(fee) or fee < 0:
            raise ValueError("fee must be finite and non-negative")
        if event_time < order.updated_at:
            raise ValueError("fill event cannot move backwards")
        cumulative_quantity = order.cumulative_filled_quantity + fill_quantity
        if cumulative_quantity > order.quantity + 1e-12:
            raise ExecutionBrokerError("simulated fill exceeds requested quantity")
        cumulative_quote = order.cumulative_quote_value + fill_quantity * fill_price
        average = cumulative_quote / cumulative_quantity
        new_state = (
            OrderState.FILLED
            if math.isclose(cumulative_quantity, order.quantity, rel_tol=1e-12, abs_tol=1e-12)
            else OrderState.PARTIALLY_FILLED
        )
        return self._update(
            order,
            new_state=new_state,
            event_time=event_time,
            broker_order_id=order.broker_order_id or self._broker_order_id(order),
            cumulative_filled_quantity=cumulative_quantity,
            cumulative_quote_value=cumulative_quote,
            cumulative_fee=order.cumulative_fee + fee,
            average_fill_price=average,
            evidence={
                "operation": "fill",
                "fill_quantity": fill_quantity,
                "fill_price": fill_price,
                "fill_fee": fee,
            },
        )

    def reject(
        self,
        order: ExecutionOrder,
        *,
        event_time: int,
        reason: str,
    ) -> OrderUpdate:
        if order.state not in {OrderState.DISPATCHED, OrderState.ACKNOWLEDGED}:
            raise ExecutionBrokerError(f"order cannot be rejected from {order.state.value}")
        if not reason:
            raise ValueError("rejection reason must not be empty")
        return self._update(
            order,
            new_state=OrderState.REJECTED,
            event_time=event_time,
            broker_order_id=order.broker_order_id or self._broker_order_id(order),
            evidence={"operation": "reject", "reason": reason},
        )

    def cancel(
        self,
        order: ExecutionOrder,
        *,
        event_time: int,
        reason: str,
    ) -> tuple[OrderUpdate, ...]:
        if order.state not in {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.CANCEL_PENDING,
        }:
            raise ExecutionBrokerError(f"order cannot be canceled from {order.state.value}")
        if not reason:
            raise ValueError("cancellation reason must not be empty")
        broker_order_id = order.broker_order_id or self._broker_order_id(order)
        canceled = self._update(
            order,
            new_state=OrderState.CANCELED,
            event_time=event_time,
            broker_order_id=broker_order_id,
            evidence={"operation": "cancel", "reason": reason},
        )
        if order.state is OrderState.CANCEL_PENDING:
            return (canceled,)
        pending = self._update(
            order,
            new_state=OrderState.CANCEL_PENDING,
            event_time=event_time,
            broker_order_id=broker_order_id,
            evidence={"operation": "cancel_pending", "reason": reason},
        )
        return pending, canceled
