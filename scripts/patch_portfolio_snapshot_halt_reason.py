from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 1:
        path.write_text(text.replace(old, new), encoding="utf-8")
        return
    if count == 0 and new in text:
        return
    raise SystemExit(
        f"expected one Portfolio snapshot source or existing target, found {count}"
    )


path = Path("src/crypto_quant/portfolio_engine.py")
replace_once(
    path,
    '''        halt_reasons = []
        for position in positions:
''',
    '''        halt_reasons = []
        if instance.status is AccountStatus.HALTED:
            reason = instance.halt_reason or ""
            if reason.startswith("reconciliation:"):
                halt_reasons.append(AccountHaltReason.RECONCILIATION_FAILURE)
            elif "accounting" in reason:
                halt_reasons.append(AccountHaltReason.ACCOUNTING_INVARIANT)
            elif "negative" in reason:
                halt_reasons.append(AccountHaltReason.NEGATIVE_ASSET)
            elif "execution" in reason:
                halt_reasons.append(AccountHaltReason.EXECUTION_CONFLICT)
            else:
                halt_reasons.append(AccountHaltReason.RECONCILIATION_FAILURE)
        for position in positions:
''',
)
replace_once(
    path,
    '''        self.health.last_snapshot_id = commit.snapshot.snapshot_id
        self.health.last_error = None
        return commit.snapshot
''',
    '''        self.health.last_snapshot_id = commit.snapshot.snapshot_id
        if AccountHaltReason.RECONCILIATION_FAILURE in commit.snapshot.halt_reasons:
            if self.health.last_error is None:
                self.health.last_error = (
                    "PortfolioHealthError: account halted by reconciliation failure"
                )
        else:
            self.health.last_error = None
        return commit.snapshot
''',
)
