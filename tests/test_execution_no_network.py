import ast
from pathlib import Path

EXECUTION_MODULES = (
    "execution.py",
    "execution_broker.py",
    "execution_contracts.py",
    "execution_engine.py",
    "execution_reconcile.py",
    "execution_runtime.py",
    "execution_store.py",
    "shadow.py",
)

FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "ccxt",
    "http",
    "httpx",
    "requests",
    "socket",
    "urllib",
    "websockets",
}

FORBIDDEN_CALL_NAMES = {
    "create_order",
    "cancel_order",
    "cancel_all_orders",
    "edit_order",
    "modify_order",
    "submit_order",
    "place_order",
    "create_orders",
    "cancel_orders",
}

FORBIDDEN_CREDENTIAL_NAMES = {
    "api_key",
    "apikey",
    "api_secret",
    "secret_key",
    "private_key",
}


def module_paths() -> list[Path]:
    root = Path(__file__).parents[1] / "src" / "crypto_quant"
    return [root / name for name in EXECUTION_MODULES]


def imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0].lower() for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0].lower())
    return roots


def call_name(node: ast.Call) -> str | None:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id.lower()
    if isinstance(target, ast.Attribute):
        return target.attr.lower()
    return None


def defined_or_assigned_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name.lower())
        elif isinstance(node, ast.Name) and isinstance(
            node.ctx,
            (ast.Store, ast.Param),
        ):
            names.add(node.id.lower())
        elif isinstance(node, ast.arg):
            names.add(node.arg.lower())
    return names


def test_execution_modules_import_no_network_or_exchange_write_library() -> None:
    violations: dict[str, list[str]] = {}
    for path in module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = sorted(imported_roots(tree) & FORBIDDEN_IMPORT_ROOTS)
        if forbidden:
            violations[path.name] = forbidden
    assert violations == {}


def test_execution_modules_define_or_call_no_real_order_write_primitive() -> None:
    violations: dict[str, list[str]] = {}
    for path in module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = {
            name
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (name := call_name(node)) is not None
            and name in FORBIDDEN_CALL_NAMES
        }
        definitions = defined_or_assigned_names(tree) & FORBIDDEN_CALL_NAMES
        forbidden = sorted(calls | definitions)
        if forbidden:
            violations[path.name] = forbidden
    assert violations == {}


def test_execution_modules_have_no_trading_credential_path() -> None:
    violations: dict[str, list[str]] = {}
    for path in module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = sorted(defined_or_assigned_names(tree) & FORBIDDEN_CREDENTIAL_NAMES)
        if forbidden:
            violations[path.name] = forbidden
    assert violations == {}
