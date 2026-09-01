from __future__ import annotations

import ast
from pathlib import Path


IDEMPOTENT = '''def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 1:
        file.write_text(text.replace(old, new), encoding="utf-8")
        return
    if count == 0 and new in text:
        return
    raise SystemExit(f"expected one source or existing target in {path}, found {count}")
'''

FILES = (
    "scripts/patch_portfolio_lifecycle.py",
    "scripts/patch_portfolio_lifecycle_failures.py",
    "scripts/patch_portfolio_execution_events.py",
    "scripts/patch_portfolio_engine_boundaries.py",
    "scripts/patch_portfolio_convergence.py",
    "scripts/patch_portfolio_audit_integration.py",
    "scripts/patch_legacy_paper_boundary.py",
)


def _replace_once_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "replace_once"
    ]


def _normalize_helper(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        raise SystemExit(f"cannot parse Portfolio patch {path}: {exc}") from exc

    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "replace_once"
    ]
    calls = _replace_once_calls(tree)
    if not definitions:
        if calls:
            raise SystemExit(f"{path} calls replace_once without a local helper")
        return
    if len(definitions) != 1:
        raise SystemExit(f"{path} must define exactly one replace_once helper")

    node = definitions[0]
    if isinstance(node, ast.AsyncFunctionDef):
        raise SystemExit(f"{path} replace_once helper must be synchronous")
    if node.end_lineno is None:
        raise SystemExit(f"cannot locate replace_once helper end in {path}")

    lines = text.splitlines(keepends=True)
    start = sum(len(line) for line in lines[: node.lineno - 1])
    end = sum(len(line) for line in lines[: node.end_lineno])
    helper = text[start:end]

    if (
        "if count == 1:" in helper
        and "if count == 0 and new in text:" in helper
        and "file.write_text(text.replace(old, new)" in helper
    ):
        return

    legacy_markers = (
        "count = text.count(old)",
        "if count != 1:",
        "file.write_text(text.replace(old, new), encoding=\"utf-8\")",
    )
    missing = [marker for marker in legacy_markers if marker not in helper]
    if missing:
        raise SystemExit(
            f"{path} has an unrecognized replace_once helper; missing: {', '.join(missing)}"
        )

    path.write_text(text[:start] + IDEMPOTENT.rstrip("\n") + text[end:], encoding="utf-8")


for name in FILES:
    _normalize_helper(Path(name))

# patch_portfolio_runtime_audit_propagation.py intentionally owns a richer
# idempotent helper set (replace_once, replace_all and append_once). It is a
# separate finalization stage and must not be rewritten by this normalizer.
finalization = Path("scripts/patch_portfolio_runtime_audit_propagation.py")
finalization_text = finalization.read_text(encoding="utf-8")
for marker in (
    "def replace_once(path: str, old: str, new: str) -> None:",
    "if count == 0 and new in text:",
    "def replace_all(path: str, old: str, new: str) -> None:",
    "def append_once(path: str, marker: str, content: str) -> None:",
):
    if marker not in finalization_text:
        raise SystemExit(f"Portfolio finalization helper is missing marker: {marker}")

round4 = Path("scripts/patch_portfolio_lifecycle_round4.py")
text = round4.read_text(encoding="utf-8")
old = '''count = store.count(old)
if count != 1:
    raise SystemExit(f"expected one critical finding halt block, found {count}")
STORE.write_text(store.replace(old, new), encoding="utf-8")
'''
new = '''count = store.count(old)
if count == 1:
    STORE.write_text(store.replace(old, new), encoding="utf-8")
elif count == 0 and new in store:
    pass
else:
    raise SystemExit(
        f"expected one critical finding source or existing target, found {count}"
    )
'''
count = text.count(old)
if count == 1:
    round4.write_text(text.replace(old, new), encoding="utf-8")
elif count == 0 and new in text:
    pass
else:
    raise SystemExit(f"expected one round4 normalization anchor, found {count}")
