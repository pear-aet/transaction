from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(".")


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise SystemExit(f"missing class {name}")


def method_node(node: ast.ClassDef, name: str):
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
            return item
    return None


def insert_lines(text: str, after_line: int, addition: str) -> str:
    lines = text.splitlines(keepends=True)
    lines.insert(after_line, addition)
    return "".join(lines)


def run_fallback(path: str) -> None:
    source = ROOT / path
    if not source.exists():
        raise SystemExit(f"missing fallback patch: {path}")
    namespace = {"__name__": f"__fallback_{source.stem}__"}
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)


def ensure_generated_components() -> None:
    if not (ROOT / "src/crypto_quant/execution_runtime.py").exists():
        run_fallback("scripts/finalize_execution_runtime.py")
    if not (ROOT / "src/crypto_quant/execution_local_broker.py").exists():
        run_fallback("scripts/harden_execution_recovery.py")
    if not (ROOT / "tests/test_execution_shadow_compat.py").exists():
        run_fallback("scripts/migrate_shadow_execution.py")


def ensure_outbox_serialization() -> None:
    path = ROOT / "src/crypto_quant/execution_engine.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    engine = class_node(tree, "ExecutionEngine")
    init = method_node(engine, "__init__")
    if init is None:
        raise SystemExit("ExecutionEngine has no __init__")
    if "self._outbox_lock = asyncio.Lock()" not in text:
        lines = text.splitlines(keepends=True)
        inserted = False
        for index in range(init.lineno - 1, init.end_lineno or len(lines)):
            if "self._processing_lock = asyncio.Lock()" in lines[index]:
                indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
                lines.insert(index + 1, indent + "self._outbox_lock = asyncio.Lock()\n")
                inserted = True
                break
        if not inserted:
            raise SystemExit("cannot locate ExecutionEngine processing lock")
        text = "".join(lines)

    tree = ast.parse(text)
    engine = class_node(tree, "ExecutionEngine")
    if method_node(engine, "_flush_outbox_unlocked") is None:
        flush = method_node(engine, "flush_outbox")
        if not isinstance(flush, ast.AsyncFunctionDef):
            raise SystemExit("ExecutionEngine has no async flush_outbox")
        lines = text.splitlines(keepends=True)
        start = flush.lineno - 1
        end = flush.end_lineno or len(lines)
        block = "".join(lines[start:end])
        block = re.sub(
            r"^(\s*)async def flush_outbox\(",
            r"\1async def _flush_outbox_unlocked(",
            block,
            count=1,
            flags=re.MULTILINE,
        )
        wrapper = '''    async def flush_outbox(
        self,
        *,
        now_ms: int | None = None,
    ) -> ExecutionBootstrapResult:
        async with self._outbox_lock:
            return await self._flush_outbox_unlocked(now_ms=now_ms)

'''
        lines[start:end] = [block + wrapper]
        text = "".join(lines)
    ast.parse(text)
    path.write_text(text, encoding="utf-8")


def command_row_converter(path: Path, text: str) -> tuple[str, str]:
    tree = ast.parse(text)
    store = class_node(tree, "ExecutionStore")
    candidates = []
    for node in store.body:
        if not isinstance(node, ast.FunctionDef) or "from_row" not in node.name:
            continue
        source = ast.get_source_segment(text, node) or ""
        score = 0
        if "ExecutionCommand" in source or "StoredExecutionCommand" in source:
            score += 4
        if "command" in node.name.lower():
            score += 2
        if "delivery_state" in source:
            score += 1
        candidates.append((score, node.name))
    if candidates:
        candidates.sort(reverse=True)
        converter = candidates[0][1]
    else:
        converter = next(
            (
                candidate
                for candidate in ("_stored_command_from_row", "_stored_from_row")
                if candidate in text
            ),
            None,
        )
    if converter is None:
        raise SystemExit(f"cannot identify command row converter in {path}")
    type_match = re.search(r"class (Stored\w*Command)\b", text)
    stored_type = "StoredExecutionCommand" if type_match is None else type_match.group(1)
    return converter, stored_type


def ensure_active_command_query() -> None:
    path = ROOT / "src/crypto_quant/execution_store.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    store = class_node(tree, "ExecutionStore")
    if method_node(store, "active_commands") is not None:
        return
    converter, stored_type = command_row_converter(path, text)
    metrics = method_node(store, "metrics")
    if metrics is None:
        raise SystemExit("ExecutionStore has no metrics method")
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
    text = insert_lines(text, metrics.lineno - 1, method)
    ast.parse(text)
    path.write_text(text, encoding="utf-8")


def reconciliation_call(method: ast.AsyncFunctionDef) -> str:
    positional = list(method.args.posonlyargs) + list(method.args.args)[1:]
    if not positional:
        raise SystemExit("reconciliation method has no command input")
    first = positional[0].arg
    if "command_id" in first or first == "id":
        command_arg = "stored.command.command_id"
    elif "stored" in first:
        command_arg = "stored"
    else:
        command_arg = "stored.command"
    args = [command_arg]
    keyword = None
    remaining = positional[1:]
    for argument in remaining:
        if argument.arg in {"now_ms", "current", "timestamp", "event_time"}:
            keyword = argument.arg
            break
    if keyword is None:
        for argument in method.args.kwonlyargs:
            if argument.arg in {"now_ms", "current", "timestamp", "event_time"}:
                keyword = argument.arg
                break
    if keyword is not None:
        args.append(f"{keyword}=current")
    elif remaining:
        args.append("current")
    return f"await self.{method.name}({', '.join(args)})"


def ensure_startup_reconciliation() -> None:
    path = ROOT / "src/crypto_quant/execution_engine.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    engine = class_node(tree, "ExecutionEngine")
    reconcile = next(
        (
            node
            for node in engine.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name.startswith("_reconcile")
            and node.name != "_reconcile_active_commands"
        ),
        None,
    )
    if reconcile is None:
        raise SystemExit("ExecutionEngine has no async command reconciliation method")
    call = reconciliation_call(reconcile)
    if method_node(engine, "reconcile_active_commands") is None:
        bootstrap = method_node(engine, "bootstrap")
        if bootstrap is None:
            raise SystemExit("ExecutionEngine has no bootstrap method")
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
            {call}
        return len(active)

'''
        text = insert_lines(text, bootstrap.lineno - 1, method)

    tree = ast.parse(text)
    engine = class_node(tree, "ExecutionEngine")
    bootstrap = method_node(engine, "bootstrap")
    if not isinstance(bootstrap, ast.AsyncFunctionDef):
        raise SystemExit("ExecutionEngine bootstrap is not async")
    block_lines = text.splitlines(keepends=True)
    start = bootstrap.lineno - 1
    end = bootstrap.end_lineno or len(block_lines)
    block = "".join(block_lines[start:end])
    required_call = "await self.reconcile_active_commands(now_ms=current)"
    if required_call not in block:
        flush_statement = None
        for statement in ast.walk(bootstrap):
            if not isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign)):
                continue
            for node in ast.walk(statement):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "flush_outbox"
                ):
                    flush_statement = statement
                    break
            if flush_statement is not None:
                break
        if flush_statement is None or flush_statement.end_lineno is None:
            raise SystemExit("cannot locate complete bootstrap outbox flush statement")
        absolute_index = flush_statement.end_lineno
        source_line = block_lines[flush_statement.lineno - 1]
        indent = source_line[: len(source_line) - len(source_line.lstrip())]
        block_lines.insert(absolute_index, indent + required_call + "\n")
        text = "".join(block_lines)
    ast.parse(text)
    path.write_text(text, encoding="utf-8")


def rewrite_counting_brokers() -> None:
    replacement = '''class CountingBroker(DeterministicLocalBroker):
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(self, *args, **kwargs):
        self.calls += 1
        return super().dispatch(*args, **kwargs)

'''
    for filename in (
        "tests/test_execution_engine_outbox.py",
        "tests/test_execution_runtime.py",
    ):
        path = ROOT / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "from crypto_quant.execution_local_broker import DeterministicLocalBroker" not in text:
            imports = list(ast.parse(text).body)
            last_import = max(
                node.end_lineno or node.lineno
                for node in imports
                if isinstance(node, (ast.Import, ast.ImportFrom))
            )
            text = insert_lines(
                text,
                last_import,
                "from crypto_quant.execution_local_broker import DeterministicLocalBroker\n",
            )
        match = re.search(
            r"^class CountingBroker\b.*?(?=^def |^async def |\Z)",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        if match is not None:
            text = text[: match.start()] + replacement + text[match.end() :]
        ast.parse(text)
        path.write_text(text, encoding="utf-8")


def harden_local_broker() -> None:
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


def canonicalize_shadow_adapter() -> None:
    path = ROOT / "src/crypto_quant/shadow.py"
    text = path.read_text(encoding="utf-8")
    marker = "# Execution Engine compatibility boundary."
    if marker in text:
        text = text[: text.index(marker)].rstrip() + "\n"
    text += '''

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
    from .execution_contracts import ExecutionContext, ExecutionMode, ExecutionPolicy

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
    ast.parse(text)
    path.write_text(text, encoding="utf-8")


def require_files() -> None:
    required = (
        "src/crypto_quant/execution_runtime.py",
        "src/crypto_quant/execution_local_broker.py",
        "tests/test_execution_engine_outbox.py",
        "tests/test_execution_runtime.py",
        "tests/test_execution_local_broker.py",
        "tests/test_execution_recovery.py",
        "tests/test_execution_shadow_compat.py",
        "docs/EXECUTION_ENGINE.md",
        "docs/EXECUTION_ENGINE_ACCEPTANCE.md",
    )
    missing = [path for path in required if not (ROOT / path).exists()]
    if missing:
        raise SystemExit("missing Execution stage files: " + ", ".join(missing))


def ensure_architecture_section() -> None:
    path = ROOT / "docs/ARCHITECTURE.md"
    text = path.read_text(encoding="utf-8")
    if "## Execution Engine stage" not in text:
        text += '''

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
        path.write_text(text, encoding="utf-8")


ensure_generated_components()
ensure_outbox_serialization()
ensure_active_command_query()
ensure_startup_reconciliation()
rewrite_counting_brokers()
harden_local_broker()
canonicalize_shadow_adapter()
require_files()
ensure_architecture_section()
