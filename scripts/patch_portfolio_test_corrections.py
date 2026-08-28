from pathlib import Path

path = Path("tests/test_portfolio_store.py")
text = path.read_text(encoding="utf-8")
old = '''def test_snapshot_cannot_claim_unstored_position_or_balances(tmp_path) -> None:
    store, _, initialized = initialize_store(tmp_path)
    decision = risk_decision()
    command = execution_command(decision)
    snapshot = build_account_snapshot(
        account_id="paper",
        observed_at=NOW,
        balances=replace(initialized.balances, cash=Decimal("9999")),
'''
new = '''def test_snapshot_cannot_claim_unstored_position_or_balances(tmp_path) -> None:
    store, _, initialized = initialize_store(tmp_path)
    snapshot = build_account_snapshot(
        account_id="paper",
        observed_at=NOW,
        balances=replace(
            initialized.balances,
            cash=Decimal("9999"),
            external_capital=Decimal("9999"),
        ),
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one snapshot-boundary test match, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
