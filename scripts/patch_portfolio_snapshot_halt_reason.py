from pathlib import Path


path = Path("src/crypto_quant/portfolio_engine.py")
text = path.read_text(encoding="utf-8")
old = '''        halt_reasons = []
        for position in positions:
'''
new = '''        halt_reasons = []
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
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one Portfolio snapshot halt anchor, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
