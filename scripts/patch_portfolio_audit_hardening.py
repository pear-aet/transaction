from pathlib import Path


path = Path("src/crypto_quant/portfolio_audit.py")
text = path.read_text(encoding="utf-8")
old = '''        with self._connect() as connection:
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if instance_row is None:
                raise PortfolioAuditError(f"account not found: {account_id}")
            state_time = int(instance_row["updated_at"])
'''
new = '''        with self._connect() as connection:
            connection.execute("BEGIN")
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if instance_row is None:
                raise PortfolioAuditError(f"account not found: {account_id}")
            state_time = int(instance_row["updated_at"])
            if observed_at < state_time:
                raise ValueError("Portfolio audit cannot precede account state")
'''
count = text.count(old)
if count == 1:
    path.write_text(text.replace(old, new), encoding="utf-8")
elif count == 0 and new in text:
    pass
else:
    raise SystemExit(
        f"expected one Portfolio audit source or existing target, found {count}"
    )
