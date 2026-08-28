from pathlib import Path

path = Path("scripts/patch_portfolio_lifecycle.py")
text = path.read_text(encoding="utf-8")
old = '''            if finding.severity is ReconciliationSeverity.CRITICAL:
                connection.execute(
                    """UPDATE account_instances SET
                    status='halted',halt_reason=?,updated_at=? WHERE account_id=?""",
                    (
                        f"reconciliation:{finding.kind.value}:{finding.finding_id}",
                        finding.found_at,
                        finding.account_id,
                    ),
                )
'''
new = '''            if finding.severity is ReconciliationSeverity.CRITICAL:
                instance_row = connection.execute(
                    "SELECT * FROM account_instances WHERE account_id=?",
                    (finding.account_id,),
                ).fetchone()
                if instance_row is None:
                    raise KeyError(f"account not found: {finding.account_id}")
                instance = self._instance_from_row(instance_row)
                halt_updated_at = max(finding.found_at, instance.updated_at)
                connection.execute(
                    """UPDATE account_instances SET
                    status='halted',halt_reason=?,updated_at=? WHERE account_id=?""",
                    (
                        f"reconciliation:{finding.kind.value}:{finding.finding_id}",
                        halt_updated_at,
                        finding.account_id,
                    ),
                )
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one finding halt block, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
