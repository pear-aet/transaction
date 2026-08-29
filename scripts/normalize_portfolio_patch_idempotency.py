from pathlib import Path


STANDARD = '''def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")
'''
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
    "scripts/patch_portfolio_runtime_audit_propagation.py",
    "scripts/patch_legacy_paper_boundary.py",
)

for name in FILES:
    path = Path(name)
    text = path.read_text(encoding="utf-8")
    if IDEMPOTENT in text:
        continue
    count = text.count(STANDARD)
    if count != 1:
        raise SystemExit(f"expected one replace_once helper in {name}, found {count}")
    path.write_text(text.replace(STANDARD, IDEMPOTENT), encoding="utf-8")
