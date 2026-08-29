from pathlib import Path


def replace_count(path: Path, old: str, new: str, expected: int) -> None:
    text = path.read_text(encoding="utf-8")
    old_count = text.count(old)
    new_count = text.count(new)
    if old_count == expected:
        path.write_text(text.replace(old, new), encoding="utf-8")
        return
    if old_count == 0 and new_count == expected:
        return
    raise SystemExit(
        f"expected {expected} authoritative-default source or targets in {path}, "
        f"found old={old_count}, new={new_count}"
    )


replace_count(
    Path("src/crypto_quant/backtest.py"),
    'accounting_mode: str = "legacy",',
    'accounting_mode: str = "portfolio",',
    2,
)
replace_count(
    Path("src/crypto_quant/cli.py"),
    '''        choices=("legacy", "portfolio"),
        default="legacy",
''',
    '''        choices=("legacy", "portfolio"),
        default="portfolio",
''',
    2,
)
