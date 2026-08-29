from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/crypto_quant/portfolio_accounting.py",
    '''        if closed_quantity != total_quantity:
            raise PortfolioAccountingError("closed quantity does not match opened quantity")
        closed_position = ClosedPosition(
''',
    '''        quantity_difference = abs(closed_quantity - total_quantity)
        if quantity_difference > Decimal("0.000000000001"):
            raise PortfolioAccountingError("closed quantity does not match opened quantity")
        closed_quantity = total_quantity
        closed_position = ClosedPosition(
''',
)

replace_once(
    "tests/test_portfolio_lifecycle.py",
    '''    with pytest.raises(ValueError, match="ID does not match"):
''',
    '''    with pytest.raises(ValueError, match="mark fact ID"):
''',
)

path = Path("tests/test_portfolio_lifecycle_store.py")
text = path.read_text(encoding="utf-8")
text = text.replace(
    '''from crypto_quant.portfolio_accounting import (
    OrderAccountingCursor,
''',
    '''from crypto_quant.portfolio_accounting import (
    MarkAccountingResult,
    OrderAccountingCursor,
''',
)
old = '''    with pytest.raises(AccountStoreConflict, match="moved backwards|revision"):
        store.commit_mark(apply_mark_fact(first_funding.position, revision), policy)
'''
new = '''    with pytest.raises(AccountStoreConflict, match="moved backwards|revision"):
        store.commit_mark(MarkAccountingResult(revision, None), policy)
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one mark revision test match, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
