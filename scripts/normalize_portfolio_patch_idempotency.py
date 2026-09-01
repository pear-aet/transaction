from __future__ import annotations

import ast
from pathlib import Path


IDEMPOTENT = '''def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    source_count = text.count(old)
    if new == "":
        if source_count == 1:
            file.write_text(text.replace(old, new), encoding="utf-8")
            return
        if source_count == 0:
            return
        raise SystemExit(
            f"expected one deletion source or completed deletion in {path}, found {source_count}"
        )
    target_count = text.count(new)
    if target_count == 1:
        return
    if target_count > 1:
        raise SystemExit(
            f"expected at most one existing target in {path}, found {target_count}"
        )
    if source_count == 1:
        file.write_text(text.replace(old, new), encoding="utf-8")
        return
    raise SystemExit(
        f"expected one source or existing target in {path}, found {source_count}"
    )
'''

FILES = (
    "scripts/patch_portfolio_lifecycle.py",
    "scripts/patch_portfolio_lifecycle_failures.py",
    "scripts/patch_portfolio_lifecycle_round4.py",
    "scripts/patch_portfolio_execution_events.py",
    "scripts/patch_portfolio_engine_boundaries.py",
    "scripts/patch_portfolio_convergence.py",
    "scripts/patch_portfolio_audit_integration.py",
    "scripts/patch_portfolio_funding_replay.py",
    "scripts/patch_portfolio_reservation_snapshot_boundaries.py",
    "scripts/patch_portfolio_available_balance.py",
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

    robust_markers = (
        'if new == "":',
        "source_count = text.count(old)",
        "target_count = text.count(new)",
        "if target_count == 1:",
        "if source_count == 1:",
        'file.write_text(text.replace(old, new), encoding="utf-8")',
    )
    if all(marker in helper for marker in robust_markers):
        return

    recognized_markers = (
        "read_text",
        "text.count(old)",
        "replace(old, new)",
        "write_text",
    )
    missing = [marker for marker in recognized_markers if marker not in helper]
    if missing:
        raise SystemExit(
            f"{path} has an unrecognized replace_once helper; missing: {', '.join(missing)}"
        )

    path.write_text(text[:start] + IDEMPOTENT.rstrip("\n") + text[end:], encoding="utf-8")


for name in FILES:
    _normalize_helper(Path(name))

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
new = '''target_count = store.count(new)
if target_count == 1:
    pass
elif target_count > 1:
    raise SystemExit(
        f"expected at most one critical finding target, found {target_count}"
    )
else:
    source_count = store.count(old)
    if source_count == 1:
        STORE.write_text(store.replace(old, new), encoding="utf-8")
    else:
        raise SystemExit(
            f"expected one critical finding source or existing target, found {source_count}"
        )
'''
old_v1 = '''count = store.count(old)
if count == 1:
    STORE.write_text(store.replace(old, new), encoding="utf-8")
elif count == 0 and new in store:
    pass
else:
    raise SystemExit(
        f"expected one critical finding source or existing target, found {count}"
    )
'''
if new in text:
    pass
elif old in text:
    round4.write_text(text.replace(old, new), encoding="utf-8")
elif old_v1 in text:
    round4.write_text(text.replace(old_v1, new), encoding="utf-8")
else:
    raise SystemExit("round4 normalization anchor is missing or ambiguous")
