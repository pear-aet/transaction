from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .domain import MarketRules
from .engine import EngineState


@dataclass(frozen=True, slots=True)
class ShadowOrder:
    symbol: str
    side: str
    quantity: float
    order_type: str
    stop_price: float | None
    validation: str
    submitted: bool = False


def build_shadow_orders(state: EngineState, rules: dict[str, MarketRules]) -> list[ShadowOrder]:
    """Build exchange-shaped order intents without any submission capability."""
    orders: list[ShadowOrder] = []
    for symbol, signal in state.pending.items():
        if not signal.action.startswith("enter_"):
            continue
        market = rules.get(symbol)
        validation = "rules_missing"
        if market is not None and signal.stop_price and signal.stop_price > 0:
            validation = "filters_available"
        orders.append(
            ShadowOrder(
                symbol,
                "buy" if signal.action == "enter_long" else "sell",
                0.0,
                "MARKET_INTENT",
                signal.stop_price,
                validation,
            )
        )
    return orders


def shadow_report(state: EngineState, rules: dict[str, MarketRules]) -> dict[str, Any]:
    return {
        "mode": "shadow",
        "submission_enabled": False,
        "orders": [asdict(item) for item in build_shadow_orders(state, rules)],
    }
