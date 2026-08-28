import ast
from pathlib import Path

BANNED_IMPORTS = {"aiohttp", "ccxt", "httpx", "requests", "socket", "websockets"}
BANNED_CALLS = {
    "amend_order",
    "cancel_order",
    "create_order",
    "edit_order",
    "place_order",
    "submit_order",
}


def test_execution_modules_have_no_live_network_or_exchange_write_path() -> None:
    root = Path("src/crypto_quant")
    violations = []
    for path in sorted(root.glob("execution*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in BANNED_IMPORTS:
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".", 1)[0]
                if module in BANNED_IMPORTS:
                    violations.append(f"{path}:{node.lineno}: from {node.module}")
            elif isinstance(node, ast.Call):
                function = node.func
                name = None
                if isinstance(function, ast.Name):
                    name = function.id
                elif isinstance(function, ast.Attribute):
                    name = function.attr
                if name in BANNED_CALLS:
                    violations.append(f"{path}:{node.lineno}: call {name}")
    assert violations == []
