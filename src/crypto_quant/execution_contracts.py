from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from .domain import finite_json
from .risk_contracts import RiskApprovedAction, RiskDecision, RiskDecisionStatus

_CLIENT_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,36}$")


class ExecutionMode(StrEnum):
    SHADOW = "shadow"
    SIMULATION = "simulation"


class ExecutionCommandStatus(StrEnum):
    SHADOW_RECORDED = "shadow_recorded"
    DISPATCH_PENDING = "dispatch_pending"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"


class ExecutionReason(StrEnum):
    READY = "ready"
    SHADOW_ONLY = "shadow_only"
    RISK_DECISION_EXPIRED = "risk_decision_expired"
    MARKET_SNAPSHOT_STALE = "market_snapshot_stale"
    MARKET_SNAPSHOT_FUTURE = "market_snapshot_future"
    MARKET_PRICE_DEVIATION = "market_price_deviation"
    INVALID_RISK_DECISION = "invalid_risk_decision"
    COMMAND_FAILED = "command_failed"


class OrderLegType(StrEnum):
    PRIMARY = "primary"
    PROTECTIVE_STOP = "protective_stop"


class ExecutionSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ExecutionOrderType(StrEnum):
    MARKET = "market"
    STOP_MARKET = "stop_market"


class OrderState(StrEnum):
    PLANNED = "planned"
    DISPATCHED = "dispatched"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


_TERMINAL_ORDER_STATES = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
        OrderState.FAILED,
    }
)


def _finite(value: float, name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _strict_json(value: Any, path: str = "evidence") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        _finite(value, path)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            _strict_json(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _strict_json(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains unsupported type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    policy_id: str = "shadow-simulation-default"
    version: str = "1.0.0"
    mode: ExecutionMode = ExecutionMode.SHADOW
    max_market_age_ms: int = 30_000
    max_market_deviation: float = 0.01
    command_ttl_ms: int = 30_000
    require_protective_stop: bool = True
    client_order_prefix: str = "cq"

    def __post_init__(self) -> None:
        if not self.policy_id or not self.version:
            raise ValueError("execution policy identifiers must not be empty")
        if min(self.max_market_age_ms, self.command_ttl_ms) < 0:
            raise ValueError("execution policy ages must be non-negative")
        _finite(self.max_market_deviation, "max_market_deviation")
        if not 0 <= self.max_market_deviation <= 1:
            raise ValueError("max_market_deviation must be in [0, 1]")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", self.client_order_prefix):
            raise ValueError("client_order_prefix must be exchange-safe and at most 8 chars")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["mode"] = self.mode.value
        return value


def execution_policy_hash(policy: ExecutionPolicy) -> str:
    canonical = json.dumps(
        policy.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionMarketSnapshot:
    snapshot_id: str
    symbol: str
    observed_at: int
    mark_price: float
    bid_price: float | None = None
    ask_price: float | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.symbol:
            raise ValueError("execution market identifiers must not be empty")
        if min(self.observed_at, self.sequence) < 0:
            raise ValueError("execution market counters must be non-negative")
        _finite(self.mark_price, "mark_price")
        if self.mark_price <= 0:
            raise ValueError("mark_price must be positive")
        if self.bid_price is not None:
            _finite(self.bid_price, "bid_price")
            if self.bid_price <= 0:
                raise ValueError("bid_price must be positive")
        if self.ask_price is not None:
            _finite(self.ask_price, "ask_price")
            if self.ask_price <= 0:
                raise ValueError("ask_price must be positive")
        if (
            self.bid_price is not None
            and self.ask_price is not None
            and self.bid_price > self.ask_price
        ):
            raise ValueError("bid_price cannot exceed ask_price")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExecutionOrder:
    order_id: str
    command_id: str
    client_order_id: str
    leg_type: OrderLegType
    side: ExecutionSide
    order_type: ExecutionOrderType
    quantity: float
    reduce_only: bool
    state: OrderState = OrderState.PLANNED
    trigger_price: float | None = None
    activation_order_id: str | None = None
    broker_order_id: str | None = None
    cumulative_filled_quantity: float = 0.0
    cumulative_quote_value: float = 0.0
    cumulative_fee: float = 0.0
    average_fill_price: float | None = None
    created_at: int = 0
    updated_at: int = 0
    sequence: int = 0
    storage_sequence: int | None = None
    evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("order_id", "command_id", "client_order_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not _CLIENT_ORDER_ID.fullmatch(self.client_order_id):
            raise ValueError("client_order_id is not exchange-safe")
        if min(self.created_at, self.updated_at, self.sequence) < 0:
            raise ValueError("order counters must be non-negative")
        if self.updated_at < self.created_at:
            raise ValueError("order updated_at cannot precede created_at")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("order storage_sequence must be positive")
        for name, value in (
            ("quantity", self.quantity),
            ("cumulative_filled_quantity", self.cumulative_filled_quantity),
            ("cumulative_quote_value", self.cumulative_quote_value),
            ("cumulative_fee", self.cumulative_fee),
        ):
            _finite(value, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.cumulative_filled_quantity > self.quantity + 1e-12:
            raise ValueError("filled quantity cannot exceed requested quantity")
        if self.trigger_price is not None:
            _finite(self.trigger_price, "trigger_price")
            if self.trigger_price <= 0:
                raise ValueError("trigger_price must be positive")
        if self.order_type is ExecutionOrderType.STOP_MARKET and self.trigger_price is None:
            raise ValueError("stop-market order requires a trigger price")
        if self.leg_type is OrderLegType.PRIMARY and self.activation_order_id is not None:
            raise ValueError("primary order cannot depend on another order")
        if self.leg_type is OrderLegType.PROTECTIVE_STOP:
            if not self.reduce_only or self.activation_order_id is None:
                raise ValueError("protective stop must be reduce-only and activation-bound")
        if self.cumulative_filled_quantity > 0:
            if self.average_fill_price is None or self.average_fill_price <= 0:
                raise ValueError("filled order requires a positive average fill price")
            expected_quote = self.cumulative_filled_quantity * self.average_fill_price
            if not math.isclose(
                self.cumulative_quote_value,
                expected_quote,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                raise ValueError("cumulative quote value is inconsistent with average fill price")
        elif self.average_fill_price is not None or self.cumulative_quote_value != 0:
            raise ValueError("unfilled order cannot contain fill price or quote value")
        if self.state is OrderState.PARTIALLY_FILLED:
            if not 0 < self.cumulative_filled_quantity < self.quantity:
                raise ValueError("partial fill state requires a partial quantity")
        if self.state is OrderState.FILLED and not math.isclose(
            self.cumulative_filled_quantity,
            self.quantity,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError("filled state requires the complete quantity")
        if (
            self.state
            in {
                OrderState.PLANNED,
                OrderState.DISPATCHED,
                OrderState.ACKNOWLEDGED,
            }
            and self.cumulative_filled_quantity != 0
        ):
            raise ValueError("pre-fill order state cannot contain fills")
        _strict_json(self.evidence or {})
        json.dumps(self.evidence or {}, sort_keys=True, allow_nan=False)
        if (self.evidence or {}).get("submitted") is not False:
            raise ValueError("shadow/simulation order must remain submitted=false")
        if (self.evidence or {}).get("network_write") is not False:
            raise ValueError("shadow/simulation order cannot permit network writes")

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL_ORDER_STATES

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.cumulative_filled_quantity)

    def with_storage_sequence(self, sequence: int) -> ExecutionOrder:
        if sequence <= 0:
            raise ValueError("order storage sequence must be positive")
        return replace(self, storage_sequence=sequence)

    def plan_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                "order_id": self.order_id,
                "command_id": self.command_id,
                "client_order_id": self.client_order_id,
                "leg_type": self.leg_type.value,
                "side": self.side.value,
                "order_type": self.order_type.value,
                "quantity": self.quantity,
                "reduce_only": self.reduce_only,
                "trigger_price": self.trigger_price,
                "activation_order_id": self.activation_order_id,
                "evidence": self.evidence or {},
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                "order_id": self.order_id,
                "command_id": self.command_id,
                "client_order_id": self.client_order_id,
                "leg_type": self.leg_type.value,
                "side": self.side.value,
                "order_type": self.order_type.value,
                "quantity": self.quantity,
                "reduce_only": self.reduce_only,
                "state": self.state.value,
                "trigger_price": self.trigger_price,
                "activation_order_id": self.activation_order_id,
                "broker_order_id": self.broker_order_id,
                "cumulative_filled_quantity": self.cumulative_filled_quantity,
                "cumulative_quote_value": self.cumulative_quote_value,
                "cumulative_fee": self.cumulative_fee,
                "average_fill_price": self.average_fill_price,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "sequence": self.sequence,
                "storage_sequence": self.storage_sequence,
                "evidence": self.evidence or {},
            }
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExecutionOrder:
        return cls(
            order_id=str(value["order_id"]),
            command_id=str(value["command_id"]),
            client_order_id=str(value["client_order_id"]),
            leg_type=OrderLegType(value["leg_type"]),
            side=ExecutionSide(value["side"]),
            order_type=ExecutionOrderType(value["order_type"]),
            quantity=float(value["quantity"]),
            reduce_only=bool(value["reduce_only"]),
            state=OrderState(value["state"]),
            trigger_price=(
                None if value.get("trigger_price") is None else float(value["trigger_price"])
            ),
            activation_order_id=value.get("activation_order_id"),
            broker_order_id=value.get("broker_order_id"),
            cumulative_filled_quantity=float(value.get("cumulative_filled_quantity", 0)),
            cumulative_quote_value=float(value.get("cumulative_quote_value", 0)),
            cumulative_fee=float(value.get("cumulative_fee", 0)),
            average_fill_price=(
                None
                if value.get("average_fill_price") is None
                else float(value["average_fill_price"])
            ),
            created_at=int(value.get("created_at", 0)),
            updated_at=int(value.get("updated_at", 0)),
            sequence=int(value.get("sequence", 0)),
            storage_sequence=(
                None if value.get("storage_sequence") is None else int(value["storage_sequence"])
            ),
            evidence=dict(value.get("evidence") or {}),
        )


@dataclass(frozen=True, slots=True)
class ExecutionCommand:
    command_id: str
    engine_instance_id: str
    execution_policy_id: str
    execution_policy_version: str
    execution_policy_hash: str
    mode: ExecutionMode
    account_id: str
    source_decision_id: str
    source_decision_storage_sequence: int
    symbol: str
    action: RiskApprovedAction
    created_at: int
    valid_until: int
    status: ExecutionCommandStatus
    reasons: tuple[ExecutionReason, ...]
    quantity: float
    reference_price: float
    approval_price: float
    stop_price: float | None
    leverage: float
    required_margin: float
    reduce_only: bool
    orders: tuple[ExecutionOrder, ...]
    market_snapshot_id: str
    evidence: dict[str, Any]
    sequence: int = 0
    storage_sequence: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        text_values = (
            self.command_id,
            self.engine_instance_id,
            self.execution_policy_id,
            self.execution_policy_version,
            self.execution_policy_hash,
            self.account_id,
            self.source_decision_id,
            self.symbol,
            self.market_snapshot_id,
        )
        if any(not value for value in text_values):
            raise ValueError("execution command identifiers must not be empty")
        if (
            min(
                self.source_decision_storage_sequence,
                self.created_at,
                self.valid_until,
                self.sequence,
            )
            < 0
        ):
            raise ValueError("execution command counters must be non-negative")
        if self.source_decision_storage_sequence <= 0:
            raise ValueError("source decision storage sequence must be positive")
        if self.valid_until < self.created_at:
            raise ValueError("execution command cannot expire before creation")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("command storage sequence must be positive")
        if self.schema_version <= 0:
            raise ValueError("execution command schema_version must be positive")
        if not self.reasons or len(self.reasons) != len(set(self.reasons)):
            raise ValueError("execution command requires unique reasons")
        for name, value in (
            ("quantity", self.quantity),
            ("reference_price", self.reference_price),
            ("approval_price", self.approval_price),
            ("leverage", self.leverage),
            ("required_margin", self.required_margin),
        ):
            _finite(value, name)
        if min(self.reference_price, self.approval_price) <= 0:
            raise ValueError("execution command prices must be positive")
        if self.quantity < 0 or self.leverage < 0 or self.required_margin < 0:
            raise ValueError("execution command amounts must be non-negative")
        if self.stop_price is not None:
            _finite(self.stop_price, "stop_price")
            if self.stop_price <= 0:
                raise ValueError("execution command stop_price must be positive")
        executable = self.status in {
            ExecutionCommandStatus.SHADOW_RECORDED,
            ExecutionCommandStatus.DISPATCH_PENDING,
            ExecutionCommandStatus.DISPATCHED,
            ExecutionCommandStatus.COMPLETED,
        }
        if executable:
            if self.quantity <= 0 or self.leverage < 1 or not self.orders:
                raise ValueError("executable command requires quantity, leverage and orders")
            if self.reasons not in {
                (ExecutionReason.READY,),
                (ExecutionReason.SHADOW_ONLY,),
            }:
                raise ValueError("executable command has invalid reasons")
        else:
            if self.orders or self.quantity != 0 or self.leverage != 0 or self.required_margin != 0:
                raise ValueError("non-executable command cannot contain an order plan")
        if self.mode is ExecutionMode.SHADOW:
            if self.status not in {
                ExecutionCommandStatus.SHADOW_RECORDED,
                ExecutionCommandStatus.EXPIRED,
                ExecutionCommandStatus.FAILED,
            }:
                raise ValueError("shadow command cannot enter a dispatch state")
        elif self.status is ExecutionCommandStatus.SHADOW_RECORDED:
            raise ValueError("simulation command cannot be shadow-recorded")
        if self.action in {RiskApprovedAction.OPEN_LONG, RiskApprovedAction.OPEN_SHORT}:
            if self.reduce_only or self.stop_price is None:
                raise ValueError("entry command requires a stop and non-reduce-only primary action")
            if len(self.orders) != 2 and executable:
                raise ValueError("entry command requires primary and protective-stop orders")
        else:
            if not self.reduce_only or self.stop_price is not None:
                raise ValueError("exit command must be reduce-only without a new stop")
            if len(self.orders) != 1 and executable:
                raise ValueError("exit command requires exactly one primary order")
        order_ids = [order.order_id for order in self.orders]
        client_ids = [order.client_order_id for order in self.orders]
        if len(order_ids) != len(set(order_ids)) or len(client_ids) != len(set(client_ids)):
            raise ValueError("execution command contains duplicate order identifiers")
        if any(order.command_id != self.command_id for order in self.orders):
            raise ValueError("execution order belongs to another command")
        primary = [order for order in self.orders if order.leg_type is OrderLegType.PRIMARY]
        if executable and len(primary) != 1:
            raise ValueError("execution command requires one primary order")
        if primary:
            if primary[0].quantity != self.quantity or primary[0].reduce_only != self.reduce_only:
                raise ValueError(
                    "primary order does not match command quantity or reduce-only flag"
                )
        _strict_json(self.evidence)
        json.dumps(self.evidence, sort_keys=True, allow_nan=False)
        if self.evidence.get("submitted") is not False:
            raise ValueError("execution command must remain submitted=false")
        if self.evidence.get("network_write") is not False:
            raise ValueError("execution command cannot permit network writes")

    def with_storage_sequence(self, sequence: int) -> ExecutionCommand:
        if sequence <= 0:
            raise ValueError("command storage sequence must be positive")
        return replace(self, storage_sequence=sequence)

    def semantic_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                "command_id": self.command_id,
                "engine_instance_id": self.engine_instance_id,
                "execution_policy_id": self.execution_policy_id,
                "execution_policy_version": self.execution_policy_version,
                "execution_policy_hash": self.execution_policy_hash,
                "mode": self.mode.value,
                "account_id": self.account_id,
                "source_decision_id": self.source_decision_id,
                "source_decision_storage_sequence": self.source_decision_storage_sequence,
                "symbol": self.symbol,
                "action": self.action.value,
                "valid_until": self.valid_until,
                "status": self.status.value,
                "reasons": [reason.value for reason in self.reasons],
                "quantity": self.quantity,
                "reference_price": self.reference_price,
                "approval_price": self.approval_price,
                "stop_price": self.stop_price,
                "leverage": self.leverage,
                "required_margin": self.required_margin,
                "reduce_only": self.reduce_only,
                "orders": [order.plan_dict() for order in self.orders],
                "market_snapshot_id": self.market_snapshot_id,
                "evidence": self.evidence,
                "schema_version": self.schema_version,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                **self.semantic_dict(),
                "orders": [order.to_dict() for order in self.orders],
                "created_at": self.created_at,
                "sequence": self.sequence,
                "storage_sequence": self.storage_sequence,
            }
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExecutionCommand:
        return cls(
            command_id=str(value["command_id"]),
            engine_instance_id=str(value["engine_instance_id"]),
            execution_policy_id=str(value["execution_policy_id"]),
            execution_policy_version=str(value["execution_policy_version"]),
            execution_policy_hash=str(value["execution_policy_hash"]),
            mode=ExecutionMode(value["mode"]),
            account_id=str(value["account_id"]),
            source_decision_id=str(value["source_decision_id"]),
            source_decision_storage_sequence=int(value["source_decision_storage_sequence"]),
            symbol=str(value["symbol"]),
            action=RiskApprovedAction(value["action"]),
            created_at=int(value["created_at"]),
            valid_until=int(value["valid_until"]),
            status=ExecutionCommandStatus(value["status"]),
            reasons=tuple(ExecutionReason(reason) for reason in value["reasons"]),
            quantity=float(value["quantity"]),
            reference_price=float(value["reference_price"]),
            approval_price=float(value["approval_price"]),
            stop_price=(None if value.get("stop_price") is None else float(value["stop_price"])),
            leverage=float(value["leverage"]),
            required_margin=float(value["required_margin"]),
            reduce_only=bool(value["reduce_only"]),
            orders=tuple(ExecutionOrder.from_dict(order) for order in value.get("orders", [])),
            market_snapshot_id=str(value["market_snapshot_id"]),
            evidence=dict(value.get("evidence") or {}),
            sequence=int(value.get("sequence", 0)),
            storage_sequence=(
                None if value.get("storage_sequence") is None else int(value["storage_sequence"])
            ),
            schema_version=int(value.get("schema_version", 1)),
        )


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    update_id: str
    order_id: str
    event_time: int
    new_state: OrderState
    cumulative_filled_quantity: float
    cumulative_quote_value: float
    cumulative_fee: float
    average_fill_price: float | None = None
    broker_order_id: str | None = None
    evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.update_id or not self.order_id:
            raise ValueError("order update identifiers must not be empty")
        if self.event_time < 0:
            raise ValueError("order update event_time must be non-negative")
        for name, value in (
            ("cumulative_filled_quantity", self.cumulative_filled_quantity),
            ("cumulative_quote_value", self.cumulative_quote_value),
            ("cumulative_fee", self.cumulative_fee),
        ):
            _finite(value, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.cumulative_filled_quantity > 0:
            if self.average_fill_price is None or self.average_fill_price <= 0:
                raise ValueError("filled update requires an average fill price")
            expected_quote = self.cumulative_filled_quantity * self.average_fill_price
            if not math.isclose(
                self.cumulative_quote_value,
                expected_quote,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                raise ValueError("order update quote value is inconsistent with average fill price")
        elif self.average_fill_price is not None or self.cumulative_quote_value != 0:
            raise ValueError("unfilled update cannot contain fill values")
        _strict_json(self.evidence or {})

    def to_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                "update_id": self.update_id,
                "order_id": self.order_id,
                "event_time": self.event_time,
                "new_state": self.new_state.value,
                "cumulative_filled_quantity": self.cumulative_filled_quantity,
                "cumulative_quote_value": self.cumulative_quote_value,
                "cumulative_fee": self.cumulative_fee,
                "average_fill_price": self.average_fill_price,
                "broker_order_id": self.broker_order_id,
                "evidence": self.evidence or {},
            }
        )


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    engine_instance_id: str
    decision: RiskDecision
    market: ExecutionMarketSnapshot
    policy: ExecutionPolicy
    planned_at: int

    def __post_init__(self) -> None:
        if not self.engine_instance_id:
            raise ValueError("execution engine_instance_id must not be empty")
        if self.planned_at < 0:
            raise ValueError("planned_at must be non-negative")
        if self.decision.status is not RiskDecisionStatus.APPROVED:
            raise ValueError("execution requires an approved risk decision")
        if self.decision.storage_sequence is None:
            raise ValueError("execution requires a persisted risk decision")
        if self.decision.symbol != self.market.symbol:
            raise ValueError("risk decision and execution market symbols differ")
