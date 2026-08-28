from pathlib import Path

path = Path("tests/test_execution_recovery.py")
text = path.read_text(encoding="utf-8")
old = "    ExecutionPolicy,\n    OrderState,\n"
new = "    ExecutionPolicy,\n    OrderLegType,\n    OrderState,\n"
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one recovery import match, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
