from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .domain import MarketRules
from .engine import EngineState
from .execution_contracts import (
    ExecutionCommand,
    ExecutionCommandStatus,
    ExecutionMode,
)


@dataclass(frozen=True, slots=True)
class ShadowOrder:
    symbol: str
    side: str
    quantity: float
    order_type: str
    stop_price: float | None
    validation: str
    submitted: bool = False
    network_write: bool = False
    command_id: str | None = None
    order_id: str | None = None
    client_order_id: str | None = None
    reduce_only: bool = False
    trigger_price: float | None = None
    state: str | None = None
    source_decision_id: str | None = None
    risk_approved: bool = False
    executable_plan: bool = False


@dataclass(frozen=True, slots=True)
class LegacyShadowIntent:
    symbol: str
    action: str
    side: str
    stop_price: float | None
    validation: str
    risk_approved: bool = False
    executable: bool = False
    submitted: bool = False
    network_write: bool = False


def build_shadow_orders(state: EngineState, rules: dict[str, MarketRules]) -> list[ShadowOrder]:
    """Compatibility view for legacy strategy signals; never an executable order plan."""
    orders: list[ShadowOrder] = []
    for symbol, signal in sorted(state.pending.items()):
        if not signal.action.startswith("enter_"):
            continue
        market = rules.get(symbol)
        validation = "rules_missing"
        if market is not None and signal.stop_price and signal.stop_price > 0:
            validation = "unapproved_strategy_signal"
        orders.append(
            ShadowOrder(
                symbol=symbol,
                side="buy" if signal.action == "enter_long" else "sell",
                quantity=0.0,
                order_type="STRATEGY_INTENT",
                stop_price=signal.stop_price,
                validation=validation,
                risk_approved=False,
                executable_plan=False,
            )
        )
    return orders


def legacy_shadow_intents(
    state: EngineState,
    rules: dict[str, MarketRules],
) -> list[LegacyShadowIntent]:
    return [
        LegacyShadowIntent(
            symbol=item.symbol,
            action=("enter_long" if item.side == "buy" else "enter_short"),
            side=item.side,
            stop_price=item.stop_price,
            validation=item.validation,
        )
        for item in build_shadow_orders(state, rules)
    ]


def build_execution_shadow_orders(
    commands: Iterable[ExecutionCommand],
) -> list[ShadowOrder]:
    """Render only persisted, risk-approved shadow commands as formal order plans."""
    rendered: list[ShadowOrder] = []
    for command in sorted(commands, key=lambda item: int(item.storage_sequence or 0)):
        if command.mode is not ExecutionMode.SHADOW:
            raise ValueError("formal shadow report accepts shadow commands only")
        if command.status is not ExecutionCommandStatus.SHADOW_RECORDED:
            raise ValueError("formal shadow report accepts recorded shadow commands only")
        if command.storage_sequence is None:
            raise ValueError("formal shadow report requires persisted execution commands")
        if command.evidence.get("submitted") is not False:
            raise ValueError("shadow command must remain submitted=false")
        if command.evidence.get("network_write") is not False:
            raise ValueError("shadow command cannot permit network writes")
        for order in command.orders:
            rendered.append(
                ShadowOrder(
                    symbol=command.symbol,
                    side=order.side.value,
                    quantity=order.quantity,
                    order_type=order.order_type.value,
                    stop_price=command.stop_price,
                    validation="persisted_risk_approved_execution_command",
                    command_id=command.command_id,
                    order_id=order.order_id,
                    client_order_id=order.client_order_id,
                    reduce_only=order.reduce_only,
                    trigger_price=order.trigger_price,
                    state=order.state.value,
                    source_decision_id=command.source_decision_id,
                    risk_approved=True,
                    executable_plan=True,
                )
            )
    return rendered


def execution_shadow_report(commands: Iterable[ExecutionCommand]) -> dict[str, Any]:
    selected = tuple(commands)
    orders = build_execution_shadow_orders(selected)
    return {
        "mode": "shadow",
        "source": "persisted_execution_commands",
        "submission_enabled": False,
        "network_write_enabled": False,
        "risk_approved": True,
        "command_count": len(selected),
        "order_count": len(orders),
        "orders": [asdict(item) for item in orders],
    }


def shadow_report(state: EngineState, rules: dict[str, MarketRules]) -> dict[str, Any]:
    """Legacy CLI compatibility report that refuses to call raw signals orders."""
    intents = legacy_shadow_intents(state, rules)
    return {
        "mode": "shadow",
        "source": "legacy_strategy_signals",
        "submission_enabled": False,
        "network_write_enabled": False,
        "risk_approved": False,
        "executable": False,
        "warning": (
            "legacy pending signals are not risk-approved ExecutionCommand records; "
            "use execution_shadow_report for formal shadow plans"
        ),
        "legacy_intents": [asdict(item) for item in intents],
        "orders": [],
    }
