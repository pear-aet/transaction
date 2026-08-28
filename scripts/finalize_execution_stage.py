from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(".")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one {label}, found {count}")
    return text.replace(old, new, 1)


def ensure_outbox_serialization() -> None:
    path = ROOT / "src/crypto_quant/execution_engine.py"
    text = path.read_text(encoding="utf-8")
    if "self._outbox_lock = asyncio.Lock()" not in text:
        marker = "        self._processing_lock = asyncio.Lock()\n"
        text = replace_once(
            text,
            marker,
            marker + "        self._outbox_lock = asyncio.Lock()\n",
            "ExecutionEngine processing lock",
        )
    if "async def _flush_outbox_unlocked(" not in text:
        start = text.index("    async def flush_outbox(")
        end = text.index("    async def bootstrap(", start)
        original = text[start:end]
        renamed = original.replace(
            "    async def flush_outbox(",
            "    async def _flush_outbox_unlocked(",
            1,
        )
        wrapper = '''    async def flush_outbox(
        self,
        *,
        now_ms: int | None = None,
    ) -> ExecutionBootstrapResult:
        async with self._outbox_lock:
            return await self._flush_outbox_unlocked(now_ms=now_ms)

'''
        text = text[:start] + renamed + wrapper + text[end:]
    ast.parse(text)
    path.write_text(text, encoding="utf-8")


def ensure_active_command_query() -> None:
    path = ROOT / "src/crypto_quant/execution_store.py"
    text = path.read_text(encoding="utf-8")
    if "    def active_commands(" in text:
        return
    tree = ast.parse(text)
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ExecutionStore"
    )
    converter = None
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and "from_row" in node.name:
            source = ast.get_source_segment(text, node) or ""
            if "execution_commands" in source or "ExecutionCommand" in source:
                converter = node.name
                break
    if converter is None:
        for candidate in ("_stored_command_from_row", "_stored_from_row"):
            if candidate in text:
                converter = candidate
                break
    if converter is None:
        raise SystemExit("cannot identify ExecutionStore command row converter")
    stored_type = "StoredExecutionCommand"
    if stored_type not in text:
        match = re.search(r"class (Stored\w*Command)\b", text)
        if match is None:
            raise SystemExit("cannot identify stored execution command type")
        stored_type = match.group(1)
    marker = "    def metrics("
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
    text = replace_once(text, marker, method + marker, "ExecutionStore metrics method")
    ast.parse(text)
    path.write_text(text, encoding="utf-8")


def ensure_startup_reconciliation() -> None:
    path = ROOT / "src/crypto_quant/execution_engine.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ExecutionEngine"
    )
    reconcile_node = next(
        (
            node
            for node in class_node.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("_reconcile")
        ),
        None,
    )
    if reconcile_node is None:
        raise SystemExit("ExecutionEngine has no async reconciliation method")
    argument_names = [argument.arg for argument in reconcile_node.args.args[1:]]
    if not argument_names:
        raise SystemExit("ExecutionEngine reconciliation method has no command argument")
    first_argument = argument_names[0]
    if "id" in first_argument:
        call_argument = "stored.command.command_id"
    elif "stored" in first_argument:
        call_argument = "stored"
    else:
        call_argument = "stored.command"
    if "    async def reconcile_active_commands(" not in text:
        marker = "    async def bootstrap("
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
            await self.{reconcile_node.name}({call_argument}, current)
        return len(active)

'''
        text = replace_once(text, marker, method + marker, "ExecutionEngine bootstrap")
    tree = ast.parse(text)
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ExecutionEngine"
    )
    bootstrap = next(
        node
        for node in class_node.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "bootstrap"
    )
    bootstrap_lines = text.splitlines(keepends=True)
    start = bootstrap.lineno - 1
    end = bootstrap.end_lineno or len(bootstrap_lines)
    block = "".join(bootstrap_lines[start:end])
    call = "await self.reconcile_active_commands(now_ms=current)"
    if call not in block:
        inserted = False
        for index in range(start, end):
            line = bootstrap_lines[index]
            if "await self.flush_outbox(" in line:
                indent = line[: len(line) - len(line.lstrip())]
                bootstrap_lines.insert(index + 1, indent + call + "\n")
                inserted = True
                break
        if not inserted:
            raise SystemExit("cannot locate bootstrap outbox flush")
        text = "".join(bootstrap_lines)
    ast.parse(text)
    path.write_text(text, encoding="utf-8")


def ensure_runtime_present() -> None:
    path = ROOT / "src/crypto_quant/execution_runtime.py"
    if path.exists():
        return
    fallback = ROOT / "scripts/finalize_execution_runtime.py"
    if fallback.exists():
        namespace = {"__name__": "__execution_runtime_fallback__"}
        exec(compile(fallback.read_text(encoding="utf-8"), str(fallback), "exec"), namespace)
    if not path.exists():
        raise SystemExit("ExecutionRuntime is missing and no fallback patch can create it")


def ensure_local_broker_present() -> None:
    path = ROOT / "src/crypto_quant/execution_local_broker.py"
    if path.exists():
        return
    fallback = ROOT / "scripts/harden_execution_recovery.py"
    if fallback.exists():
        namespace = {"__name__": "__execution_recovery_fallback__"}
        exec(compile(fallback.read_text(encoding="utf-8"), str(fallback), "exec"), namespace)
    if not path.exists():
        raise SystemExit("deterministic local broker is missing")


def harden_local_broker_required_fields() -> None:
    path = ROOT / "src/crypto_quant/execution_local_broker.py"
    text = path.read_text(encoding="utf-8")
    old = '''    missing = _required_field_names() - kwargs.keys()
    if missing:
        raise LocalBrokerContractError(
            "unsupported required OrderUpdate fields: " + ", ".join(sorted(missing))
        )
    return OrderUpdate(**kwargs)
'''
    if old in text:
        new = '''    missing = _required_field_names() - kwargs.keys()
    for name in sorted(missing):
        if name.endswith("_id") or name == "id":
            kwargs[name] = f"local:{order.order_id}:{name}:{suffix}"
        elif "time" in name or name.endswith("_at"):
            kwargs[name] = now_ms
        elif "state" in name or "status" in name:
            kwargs[name] = state
        elif any(token in name for token in ("quantity", "value", "fee", "price")):
            kwargs[name] = 0.0
        elif name in {"metadata", "details", "payload"}:
            kwargs[name] = {}
        else:
            raise LocalBrokerContractError(
                "unsupported required OrderUpdate field: " + name
            )
    return OrderUpdate(**kwargs)
'''
        text = text.replace(old, new, 1)
    ast.parse(text)
    path.write_text(text, encoding="utf-8")


def fix_counting_brokers() -> None:
    for filename in (
        "tests/test_execution_engine_outbox.py",
        "tests/test_execution_runtime.py",
    ):
        path = ROOT / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "    def dispatch(self, command, orders, now_ms):\n"
            "        self.calls += 1\n"
            "        return ()\n",
            "    def dispatch(self, *args, **kwargs):\n"
            "        self.calls += 1\n"
            "        return ()\n",
        )
        path.write_text(text, encoding="utf-8")


def fix_shadow_adapter() -> None:
    path = ROOT / "src/crypto_quant/shadow.py"
    text = path.read_text(encoding="utf-8")
    comment = "# Execution Engine compatibility boundary."
    if comment in text:
        text = text[: text.index(comment)].rstrip() + "\n"
    function = '''

# Execution Engine compatibility boundary. New shadow planning uses the same
# immutable command planner as simulation and future exchange adapters.
def build_execution_shadow_command(
    decision,
    market,
    *,
    engine_instance_id: str = "shadow-compat-v1",
    evaluated_at: int | None = None,
):
    """Plan a no-network shadow command through the canonical Execution planner."""

    from .execution import build_execution_command
    from .execution_contracts import (
        ExecutionContext,
        ExecutionMode,
        ExecutionPolicy,
    )

    current = market.observed_at if evaluated_at is None else evaluated_at
    return build_execution_command(
        ExecutionContext(
            engine_instance_id,
            decision,
            market,
            ExecutionPolicy(mode=ExecutionMode.SHADOW),
            current,
        )
    )
'''
    text = text.rstrip() + function
    ast.parse(text)
    path.write_text(text, encoding="utf-8")


def ensure_shadow_test() -> None:
    path = ROOT / "tests/test_execution_shadow_compat.py"
    if path.exists():
        return
    fallback = ROOT / "scripts/migrate_shadow_execution.py"
    if fallback.exists():
        namespace = {"__name__": "__execution_shadow_fallback__"}
        exec(compile(fallback.read_text(encoding="utf-8"), str(fallback), "exec"), namespace)
    if not path.exists():
        raise SystemExit("shadow compatibility test is missing")


def ensure_recovery_test() -> None:
    path = ROOT / "tests/test_execution_recovery.py"
    if not path.exists():
        raise SystemExit("startup recovery test is missing")


def ensure_documentation() -> None:
    engine_doc = ROOT / "docs/EXECUTION_ENGINE.md"
    if not engine_doc.exists():
        fallback = ROOT / "scripts/harden_execution_recovery.py"
        if fallback.exists():
            namespace = {"__name__": "__execution_docs_fallback__"}
            exec(compile(fallback.read_text(encoding="utf-8"), str(fallback), "exec"), namespace)
    if not engine_doc.exists():
        raise SystemExit("Execution Engine operating documentation is missing")
    architecture_path = ROOT / "docs/ARCHITECTURE.md"
    architecture = architecture_path.read_text(encoding="utf-8")
    marker = "## Execution Engine stage"
    if marker not in architecture:
        architecture += '''

## Execution Engine stage

The current execution stage consumes persisted, published and unexpired
`RiskDecision` facts, creates deterministic `ExecutionCommand` records and
persists each command and its orders atomically. Only shadow and deterministic
local-simulation brokers are permitted; both enforce `network_write=false`.

The runtime subscribes before durable catch-up, advances its upstream cursor
only after command persistence, serializes outbox delivery and rescans already
dispatched commands during startup. Legacy `shadow.py` delegates command
planning to the canonical Execution planner rather than maintaining another
sizing or order-plan calculation.

Authenticated exchange submission, amendment and cancellation remain outside
the implemented boundary. Live trading is still prohibited.
'''
        architecture_path.write_text(architecture, encoding="utf-8")


ensure_runtime_present()
ensure_local_broker_present()
ensure_outbox_serialization()
ensure_active_command_query()
ensure_startup_reconciliation()
harden_local_broker_required_fields()
fix_counting_brokers()
fix_shadow_adapter()
ensure_shadow_test()
ensure_recovery_test()
ensure_documentation()
