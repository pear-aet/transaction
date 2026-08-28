from pathlib import Path

for name in (
    "tests/test_portfolio_accounting.py",
    "tests/test_portfolio_store.py",
):
    path = Path(name)
    text = path.read_text(encoding="utf-8")
    old = "from .portfolio_fixtures import ("
    new = "from portfolio_fixtures import ("
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one fixture import in {name}, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")
