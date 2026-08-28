from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from typing import Any

from .domain import finite_json
from .execution_contracts import (
    ExecutionCommand,
    ExecutionCommandStatus,
    ExecutionContext,
    ExecutionMode,
    ExecutionOrder,
    ExecutionOrderType,
    ExecutionPolicy,
    ExecutionReason,
    ExecutionSide,
    OrderLegType,
    OrderState,
    OrderUpdate,
    execution_policy_hash,
)
from .risk_contracts import RiskApprovedAction


class ExecutionCommandMismatch(RuntimeError):
    """Raised when a planner returns a command for another execution context."""


class OrderTransitionError(RuntimeError):
    """Raised when an order update violates the monotonic state machine."""


_ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PLANNED: frozenset(
        {OrderState.DISPATCHED, OrderState.EXPIRED, OrderState.FAILED}
    ),
    OrderState.DISPATCHED: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        }
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELED,
            OrderState.FAILED,
        }
    ),
    OrderState.CANCEL_PENDING: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.FAILED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.FAILED: frozenset(),
}


def _stable_identifier(prefix: str, payload: dict[str, Any], *, size: int = 16) -> str:
    canonical = json.dumps(
        finite_json(payload),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.blake2s(canonical.encode("utf-8"), digest_size=size).hexdigest()
    return f"{prefix}:{digest}"


def _client_order_id(policy: ExecutionPolicy, command_id: str, leg: str) -> str:
    digest = hashlib.blake2s(
        f"{command_id}:{leg}".encode("utf-8"), digest_size=12
    ).hexdigest()
    return f"{policy.client_order_prefix}_{leg[0]}_{digest}"


def _primary_side(action: RiskApprovedAction) -> ExecutionSide:
    return (
        ExecutionSide.BUY
        if action in {RiskApprovedAction.OPEN_LONG, RiskApprovedAction.CLOSE_SHORT}
        else ExecutionSide.SELL
    )


def _protective_side(action: RiskApprovedAction) -> ExecutionSide:
    if action is RiskApprovedAction.OPEN_LONG:
        return ExecutionSide.SELL
    if action is RiskApprovedAction.OPEN_SHORT:
        return ExecutionSide.BUY
    raise ValueError("exit action has no protective-stop side")


def _execution_price(context: ExecutionContext) -> float:
    side = _primary_side(context.decision.approved_action)
    if side is ExecutionSide.BUY and context.market.ask_price is not None:
        return context.market.ask_price
    if side is ExecutionSide.SELL and context.market.bid_price is not None:
        return context.market.bid_price
    return context.market.mark_price


def _non_executable_command(
    context: ExecutionContext,
    *,
    status: ExecutionCommandStatus,
    reasons: tuple[ExecutionReason, ...],
) -> ExecutionCommand:
    policy_hash = execution_policy_hash(context.policy)
    valid_until = context.planned_at
    payload = {
        "engine_instance_id": context.engine_instance_id,
        "policy_hash": policy_hash,
        "source_decision_id": context.decision.decision_id,
        "source_decision_storage_sequence": context.decision.storage_sequence,
        "market_snapshot_id": context.market.snapshot_id,
        "status": status.value,
        "reasons": [reason.value for reason in reasons],
        "valid_until": valid_until,
    }
    return ExecutionCommand(
        command_id=_stable_identifier("execution_command", payload),
        engine_instance_id=context.engine_instance_id,
        execution_policy_id=context.policy.policy_id,
        execution_policy_version=context.policy.version,
        execution_policy_hash=policy_hash,
        mode=context.policy.mode,
        account_id=context.decision.account_id,
        source_decision_id=context.decision.decision_id,
        source_decision_storage_sequence=int(context.decision.storage_sequence or 0),
        symbol=context.decision.symbol,
        action=context.decision.approved_action,
        created_at=context.planned_at,
        valid_until=valid_until,
        status=status,
        reasons=reasons,
        quantity=0.0,
        reference_price=context.decision.reference_price,
        approval_price=context.decision.approval_price,
        stop_price=context.decision.stop_price,
        leverage=0.0,
        required_margin=0.0,
        reduce_only=False,
        orders=(),
        market_snapshot_id=context.market.snapshot_id,
        evidence={
            "submitted": False,
            "network_write": False,
            "source_risk_valid_until": context.decision.valid_until,
        },
    )


def build_execution_command(context: ExecutionContext) -> ExecutionCommand:
    decision = context.decision
    policy = context.policy
    market = context.market

    if context.planned_at > decision.valid_until:
        return _non_executable_command(
            context,
            status=ExecutionCommandStatus.EXPIRED,
            reasons=(ExecutionReason.RISK_DECISION_EXPIRED,),
        )
    if market.observed_at > context.planned_at:
        return _non_executable_command(
            context,
            status=ExecutionCommandStatus.FAILED,
            reasons=(ExecutionReason.MARKET_SNAPSHOT_FUTURE,),
        )
    if context.planned_at - market.observed_at > policy.max_market_age_ms:
        return _non_executable_command(
            context,
            status=ExecutionCommandStatus.EXPIRED,
            reasons=(ExecutionReason.MARKET_SNAPSHOT_STALE,),
        )

    execution_price = _execution_price(context)
    deviation = abs(execution_price - decision.approval_price) / decision.approval_price
    if deviation > policy.max_market_deviation:
        return _non_executable_command(
            context,
            status=ExecutionCommandStatus.EXPIRED,
            reasons=(ExecutionReason.MARKET_PRICE_DEVIATION,),
        )

    valid_until = min(
        decision.valid_until,
        context.planned_at + policy.command_ttl_ms,
        market.observed_at + policy.max_market_age_ms,
    )
    if valid_until < context.planned_at:
        return _non_executable_command(
            context,
            status=ExecutionCommandStatus.EXPIRED,
            reasons=(ExecutionReason.MARKET_SNAPSHOT_STALE,),
        )

    policy_hash = execution_policy_hash(policy)
    command_payload = {
        "engine_instance_id": context.engine_instance_id,
        "policy_hash": policy_hash,
        "source_decision_id": decision.decision_id,
        "source_decision_storage_sequence": decision.storage_sequence,
        "market_snapshot_id": market.snapshot_id,
        "mode": policy.mode.value,
        "action": decision.approved_action.value,
        "quantity": format(decision.quantity, ".17g"),
        "valid_until": valid_until,
        "execution_price": format(execution_price, ".17g"),
    }
    command_id = _stable_identifier("execution_command", command_payload)
    primary_id = _stable_identifier(
        "execution_order",
        {"command_id": command_id, "leg": OrderLegType.PRIMARY.value},
    )
    primary = ExecutionOrder(
        order_id=primary_id,
        command_id=command_id,
        client_order_id=_client_order_id(policy, command_id, OrderLegType.PRIMARY.value),
        leg_type=OrderLegType.PRIMARY,
        side=_primary_side(decision.approved_action),
        order_type=ExecutionOrderType.MARKET,
        quantity=decision.quantity,
        reduce_only=decision.reduce_only,
        created_at=context.planned_at,
        updated_at=context.planned_at,
        evidence={
            "submitted": False,
            "network_write": False,
            "target": policy.mode.value,
        },
    )
    orders = [primary]
    if decision.approved_action in {
        RiskApprovedAction.OPEN_LONG,
        RiskApprovedAction.OPEN_SHORT,
    }:
        if decision.stop_price is None:
            raise ValueError("approved entry risk decision has no stop price")
        if not policy.require_protective_stop:
            raise ValueError("entry execution policy must require a protective stop")
        stop_id = _stable_identifier(
            "execution_order",
            {"command_id": command_id, "leg": OrderLegType.PROTECTIVE_STOP.value},
        )
        orders.append(
            ExecutionOrder(
                order_id=stop_id,
                command_id=command_id,
                client_order_id=_client_order_id(
                    policy, command_id, OrderLegType.PROTECTIVE_STOP.value
                ),
                leg_type=OrderLegType.PROTECTIVE_STOP,
                side=_protective_side(decision.approved_action),
                order_type=ExecutionOrderType.STOP_MARKET,
                quantity=decision.quantity,
                reduce_only=True,
                trigger_price=decision.stop_price,
                activation_order_id=primary.order_id,
                created_at=context.planned_at,
                updated_at=context.planned_at,
                evidence={
                    "submitted": False,
                    "network_write": False,
                    "target": policy.mode.value,
                },
            )
        )

    status = (
        ExecutionCommandStatus.SHADOW_RECORDED
        if policy.mode is ExecutionMode.SHADOW
        else ExecutionCommandStatus.DISPATCH_PENDING
    )
    reason = (
        ExecutionReason.SHADOW_ONLY
        if policy.mode is ExecutionMode.SHADOW
        else ExecutionReason.READY
    )
    return ExecutionCommand(
        command_id=command_id,
        engine_instance_id=context.engine_instance_id,
        execution_policy_id=policy.policy_id,
        execution_policy_version=policy.version,
        execution_policy_hash=policy_hash,
        mode=policy.mode,
        account_id=decision.account_id,
        source_decision_id=decision.decision_id,
        source_decision_storage_sequence=int(decision.storage_sequence or 0),
        symbol=decision.symbol,
        action=decision.approved_action,
        created_at=context.planned_at,
        valid_until=valid_until,
        status=status,
        reasons=(reason,),
        quantity=decision.quantity,
        reference_price=decision.reference_price,
        approval_price=execution_price,
        stop_price=decision.stop_price,
        leverage=decision.leverage,
        required_margin=decision.required_margin,
        reduce_only=decision.reduce_only,
        orders=tuple(orders),
        market_snapshot_id=market.snapshot_id,
        evidence={
            "submitted": False,
            "network_write": False,
            "market_deviation": deviation,
            "source_risk_valid_until": decision.valid_until,
        },
    )


def validate_execution_command(
    context: ExecutionContext,
    command: ExecutionCommand,
) -> ExecutionCommand:
    expected = build_execution_command(context)
    if command.semantic_dict() != expected.semantic_dict():
        fields = [
            key
            for key in expected.semantic_dict()
            if command.semantic_dict().get(key) != expected.semantic_dict().get(key)
        ]
        raise ExecutionCommandMismatch(
            "execution command mismatch: " + ", ".join(fields)
        )
    return command


def apply_order_update(order: ExecutionOrder, update: OrderUpdate) -> ExecutionOrder:
    if update.order_id != order.order_id:
        raise OrderTransitionError("order update belongs to another order")
    if update.event_time < order.updated_at:
        raise OrderTransitionError("order update time moved backwards")
    if order.terminal:
        raise OrderTransitionError(f"terminal order cannot transition from {order.state.value}")
    if update.new_state not in _ALLOWED_TRANSITIONS[order.state]:
        raise OrderTransitionError(
            f"invalid order transition: {order.state.value} -> {update.new_state.value}"
        )
    if update.cumulative_filled_quantity + 1e-12 < order.cumulative_filled_quantity:
        raise OrderTransitionError("cumulative filled quantity moved backwards")
    if update.cumulative_quote_value + 1e-12 < order.cumulative_quote_value:
        raise OrderTransitionError("cumulative quote value moved backwards")
    if update.cumulative_fee + 1e-12 < order.cumulative_fee:
        raise OrderTransitionError("cumulative fee moved backwards")
    if update.cumulative_filled_quantity > order.quantity + 1e-12:
        raise OrderTransitionError("filled quantity exceeds requested quantity")
    if order.broker_order_id is not None and update.broker_order_id not in {
        None,
        order.broker_order_id,
    }:
        raise OrderTransitionError("broker order identifier changed")
    broker_order_id = update.broker_order_id or order.broker_order_id
    evidence = {
        **(order.evidence or {}),
        "last_update_id": update.update_id,
        "last_update_evidence": update.evidence or {},
    }
    return replace(
        order,
        state=update.new_state,
        broker_order_id=broker_order_id,
        cumulative_filled_quantity=update.cumulative_filled_quantity,
        cumulative_quote_value=update.cumulative_quote_value,
        cumulative_fee=update.cumulative_fee,
        average_fill_price=update.average_fill_price,
        updated_at=update.event_time,
        evidence=evidence,
    )
