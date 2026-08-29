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
