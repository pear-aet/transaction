from pathlib import Path
import re


ROOT = Path(".")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


store_path = ROOT / "src/crypto_quant/execution_store.py"
store_text = store_path.read_text(encoding="utf-8")
if "    def active_commands(" not in store_text:
    stored_type_match = re.search(r"class (Stored\w*Command)\b", store_text)
    if stored_type_match is None:
        raise SystemExit("cannot identify stored execution command type")
    stored_type = stored_type_match.group(1)
    converter_match = re.search(
        r"def (_stored\w*(?:command)?\w*from_row)\(",
        store_text,
        flags=re.IGNORECASE,
    )
    if converter_match is None:
        for candidate in ("_stored_command_from_row", "_stored_from_row"):
            if candidate in store_text:
                converter = candidate
                break
        else:
            raise SystemExit("cannot identify execution command row converter")
    else:
        converter = converter_match.group(1)
    marker = "    def metrics("
    if store_text.count(marker) != 1:
        raise SystemExit("cannot locate ExecutionStore.metrics")
    method = f'''    def active_commands(
        self,
        *,
        engine_instance_id: str | None = None,
        limit: int = 1000,
    ) -> list[{stored_type}]:
        if not 1 <= limit <= 10_000:
            raise ValueError("active command limit must be in [1, 10000]")
        query = """SELECT * FROM execution_commands
        WHERE delivery_state='dispatched'
        AND (? IS NULL OR engine_instance_id=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (engine_instance_id, engine_instance_id, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self.{converter}(row) for row in rows]

'''
    store_text = store_text.replace(marker, method + marker, 1)
store_path.write_text(store_text, encoding="utf-8")


engine_path = ROOT / "src/crypto_quant/execution_engine.py"
engine_text = engine_path.read_text(encoding="utf-8")
if "    async def reconcile_active_commands(" not in engine_text:
    reconcile_match = re.search(
        r"    async def (?P<name>_reconcile\w*)\((?P<params>.*?)\n    \)",
        engine_text,
        flags=re.DOTALL,
    )
    if reconcile_match is None:
        reconcile_match = re.search(
            r"    async def (?P<name>_reconcile\w*)\(self,\s*(?P<params>.*?)\):",
            engine_text,
            flags=re.DOTALL,
        )
    if reconcile_match is None:
        raise SystemExit("cannot identify ExecutionEngine reconciliation method")
    reconcile_name = reconcile_match.group("name")
    params = reconcile_match.group("params")
    if "command_id" in params:
        reconcile_argument = "stored.command.command_id"
    elif "Stored" in params or "stored" in params:
        reconcile_argument = "stored"
    else:
        reconcile_argument = "stored.command"
    marker = "    async def bootstrap("
    if engine_text.count(marker) != 1:
        raise SystemExit("cannot locate ExecutionEngine.bootstrap")
    method = f'''    async def reconcile_active_commands(
        self,
        *,
        now_ms: int | None = None,
    ) -> int:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        active = await asyncio.to_thread(
            self.store.active_commands,
            engine_instance_id=self.settings.engine_instance_id,
            limit=self.settings.outbox_batch_size,
        )
        for stored in active:
            await self.{reconcile_name}({reconcile_argument}, current)
        return len(active)

'''
    engine_text = engine_text.replace(marker, method + marker, 1)

bootstrap_start = engine_text.index("    async def bootstrap(")
bootstrap_end = engine_text.find("    async def ", bootstrap_start + 10)
if bootstrap_end == -1:
    bootstrap_end = len(engine_text)
bootstrap_block = engine_text[bootstrap_start:bootstrap_end]
reconcile_call = "                await self.reconcile_active_commands(now_ms=current)\n"
if reconcile_call not in bootstrap_block:
    flush_match = re.search(
        r"(?P<indent>\s*)[^\n]*await self\.flush_outbox\(now_ms=current\)\n",
        bootstrap_block,
    )
    if flush_match is None:
        raise SystemExit("cannot locate bootstrap outbox flush")
    insert_at = bootstrap_start + flush_match.end()
    engine_text = engine_text[:insert_at] + reconcile_call + engine_text[insert_at:]
engine_path.write_text(engine_text, encoding="utf-8")


write(
    "src/crypto_quant/execution_local_broker.py",
    '''from __future__ import annotations

from dataclasses import MISSING, fields
from typing import Any

from .execution_contracts import ExecutionCommand, ExecutionOrder, OrderState, OrderUpdate


class LocalBrokerContractError(RuntimeError):
    """Raised when the local broker cannot satisfy the current update contract."""


def _required_field_names() -> set[str]:
    required: set[str] = set()
    for item in fields(OrderUpdate):
        if item.default is MISSING and item.default_factory is MISSING:
            required.add(item.name)
    return required


def _make_update(
    order: ExecutionOrder,
    state: OrderState,
    now_ms: int,
    *,
    filled_quantity: float = 0.0,
    fill_price: float | None = None,
    fee: float = 0.0,
    suffix: str,
) -> OrderUpdate:
    quote_value = 0.0 if fill_price is None else filled_quantity * fill_price
    values: dict[str, Any] = {
        "update_id": f"local:{order.order_id}:{suffix}",
        "order_id": order.order_id,
        "event_time": now_ms,
        "timestamp": now_ms,
        "occurred_at": now_ms,
        "new_state": state,
        "state": state,
        "cumulative_filled_quantity": filled_quantity,
        "cumulative_quote_value": quote_value,
        "cumulative_fee": fee,
        "average_fill_price": fill_price,
        "broker_order_id": f"local:{order.order_id}",
        "reject_reason": None,
        "reason": None,
        "evidence": {
            "broker": "deterministic_local",
            "network_write": False,
        },
    }
    names = {item.name for item in fields(OrderUpdate)}
    kwargs = {name: value for name, value in values.items() if name in names}
    missing = _required_field_names() - kwargs.keys()
    if missing:
        raise LocalBrokerContractError(
            "unsupported required OrderUpdate fields: " + ", ".join(sorted(missing))
        )
    return OrderUpdate(**kwargs)


def _activation_dependency(order: ExecutionOrder) -> str | None:
    for name in (
        "activation_order_id",
        "activation_dependency_order_id",
        "activates_after_order_id",
    ):
        value = getattr(order, name, None)
        if value:
            return str(value)
    return None


class DeterministicLocalBroker:
    """Restart-idempotent shadow/simulation broker with no network write path."""

    network_write_enabled = False

    @staticmethod
    def _resolve_dispatch_arguments(
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...] | list[ExecutionOrder] | int | None,
        now_ms: int | None,
    ) -> tuple[tuple[ExecutionOrder, ...], int]:
        if isinstance(orders, int) and now_ms is None:
            now_ms = orders
            orders = None
        if now_ms is None:
            raise ValueError("local broker dispatch requires now_ms")
        selected = tuple(command.orders if orders is None else orders)
        return selected, now_ms

    def dispatch(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...] | list[ExecutionOrder] | int | None = None,
        now_ms: int | None = None,
    ) -> tuple[OrderUpdate, ...]:
        selected, current = self._resolve_dispatch_arguments(command, orders, now_ms)
        updates = []
        for order in selected:
            if _activation_dependency(order) is not None:
                continue
            if order.state is not OrderState.PLANNED:
                continue
            updates.append(
                _make_update(
                    order,
                    OrderState.DISPATCHED,
                    current,
                    suffix="dispatched",
                )
            )
        return tuple(updates)

    def activate(
        self,
        order: ExecutionOrder,
        quantity: float | int | None = None,
        now_ms: int | None = None,
    ) -> tuple[OrderUpdate, ...]:
        if isinstance(quantity, int) and now_ms is None:
            now_ms = quantity
            quantity = None
        if now_ms is None:
            raise ValueError("local broker activation requires now_ms")
        if quantity is not None and float(quantity) <= 0:
            raise ValueError("activation quantity must be positive")
        return (
            _make_update(
                order,
                OrderState.DISPATCHED,
                now_ms,
                suffix="activated",
            ),
        )

    def fill(
        self,
        order: ExecutionOrder,
        *,
        fill_price: float,
        now_ms: int,
        fee: float = 0.0,
    ) -> OrderUpdate:
        if fill_price <= 0 or now_ms < 0 or fee < 0:
            raise ValueError("invalid deterministic local fill")
        return _make_update(
            order,
            OrderState.FILLED,
            now_ms,
            filled_quantity=order.quantity,
            fill_price=fill_price,
            fee=fee,
            suffix=f"filled:{fill_price:.12g}:{fee:.12g}",
        )
''',
)


write(
    "tests/test_execution_local_broker.py",
    '''from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution import build_execution_command
from crypto_quant.execution_contracts import (
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    OrderState,
)
from crypto_quant.execution_local_broker import DeterministicLocalBroker
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskPolicy,
)
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
NOW = 900_001


def command():
    rules = MarketRules(
        SYMBOL,
        0.1,
        0.001,
        0.001,
        5,
        100,
        (RiskBracket(0, 1_000_000, 0.005),),
    )
    intent = StrategyIntent(
        intent_id="intent-local-broker",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=900_000,
        created_at=900_000,
        reference_price=100,
        stop_price=99,
        reason="local_broker_test",
        source_event_id="market-local-broker",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=1,
    )
    account = AccountRiskSnapshot(
        "account-local-broker",
        "paper",
        900_000,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    market = MarketRiskSnapshot(
        "risk-market-local-broker",
        SYMBOL,
        900_000,
        100,
        rules,
    )
    decision = evaluate_risk(
        RiskContext("risk-primary", intent, account, market, RiskPolicy(), NOW)
    ).with_storage_sequence(1)
    execution_market = ExecutionMarketSnapshot(
        "execution-market-local-broker",
        SYMBOL,
        NOW,
        100,
        99.9,
        100.1,
    )
    return build_execution_command(
        ExecutionContext(
            "execution-primary",
            decision,
            execution_market,
            ExecutionPolicy(mode=ExecutionMode.SIMULATION),
            NOW,
        )
    )


def test_local_broker_dispatch_is_deterministic_and_never_network_writes() -> None:
    value = command()
    broker = DeterministicLocalBroker()

    first = broker.dispatch(value, NOW)
    second = broker.dispatch(value, NOW)

    assert broker.network_write_enabled is False
    assert first == second
    assert first
    assert all(update.new_state is OrderState.DISPATCHED for update in first)
    assert all(update.evidence["network_write"] is False for update in first)


def test_local_full_fill_update_is_deterministic_and_consistent() -> None:
    value = command()
    broker = DeterministicLocalBroker()
    primary = value.orders[0]

    first = broker.fill(primary, fill_price=100.1, now_ms=NOW + 1, fee=0.2)
    second = broker.fill(primary, fill_price=100.1, now_ms=NOW + 1, fee=0.2)

    assert first == second
    assert first.new_state is OrderState.FILLED
    assert first.cumulative_filled_quantity == primary.quantity
    assert first.cumulative_quote_value == primary.quantity * 100.1
    assert first.average_fill_price == 100.1
''',
)


write(
    "docs/EXECUTION_ENGINE.md",
    '''# Execution Engine

## Scope

Execution Engine consumes persisted and approved `RiskDecision` records and creates durable local execution commands. The current stage supports only:

- shadow recording;
- deterministic local simulation;
- order lifecycle persistence and reconciliation.

It contains no exchange order submission, amendment or cancellation endpoint, and it does not accept trading-permission API credentials.

## Safety boundary

A command is executable only when all of the following remain true:

- its source risk decision is approved, persisted, published and unexpired;
- the execution market snapshot is current and belongs to the same symbol;
- market deviation remains inside the execution policy;
- the generated command exactly matches the source decision and policy;
- the selected broker declares `network_write_enabled = false`.

A strategy cannot choose quantity, leverage, margin, reduce-only behavior or order type. Those fields originate from the validated risk decision and the execution policy.

## Durable lifecycle

```text
RiskDecision published
        ↓
ExecutionCommand committed atomically with orders
        ↓
recorded | pending
        ↓
local deterministic dispatch
        ↓
dispatched
        ↓
order updates appended and current order state updated atomically
        ↓
completed | expired | failed
```

`pending` commands are an outbox. The source `RiskDecision` is re-read from `RiskStore` before every dispatch. A source still in `pending` state is a hard barrier and cannot be skipped. A source that expired before dispatch causes the command to expire.

## Concurrency and idempotency

Business planning and outbox dispatch use separate locks:

- the processing lock serializes command creation for one engine instance;
- the outbox lock serializes broker dispatch and prevents concurrent duplicate delivery.

The local broker uses deterministic order and update identifiers. Repeating dispatch after process failure produces the same update facts rather than a second external side effect.

## Startup recovery

Startup performs three operations in order:

1. expire or dispatch pending commands whose source risk facts are terminal;
2. stop at unpublished source barriers;
3. rescan already-dispatched commands and derive command terminal state from persisted order state.

The third step closes the crash window where an order update was committed but the process stopped before the parent command was marked complete.

## Runtime consumption

`ExecutionRuntime` subscribes to the live risk bus before durable catch-up. The durable upstream cursor:

- defaults to published approved risk decisions;
- uses a pinned filter;
- advances one decision at a time;
- advances only after the execution command is durably present;
- may advance after a visible dispatch failure only when the command was already committed.

This gives at-least-once delivery with deterministic idempotency. The system does not claim exactly-once behavior across a future exchange API.

## Order updates

Order updates are append-only facts. Current order state is a separately maintained projection. The state machine rejects:

- cumulative fill, quote value or fee moving backwards;
- fill quantity above the approved order quantity;
- broker order identity changes;
- terminal orders returning to an active state;
- partial-fill events that add no fill;
- inconsistent average price and cumulative quote value.

## Remaining live-trading work

Real trading remains blocked until separate review and implementation of:

- authenticated exchange adapters;
- exchange-native idempotency and client-order reconciliation;
- cancel/replace behavior for partial fills;
- position and balance reconciliation;
- kill switches and operator authorization;
- secret isolation and trading-key permission controls.
''',
)
