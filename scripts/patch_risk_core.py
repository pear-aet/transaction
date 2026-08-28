from pathlib import Path

path = Path("src/crypto_quant/risk_contracts.py")
text = path.read_text(encoding="utf-8")

old = '''        if self.status is RiskDecisionStatus.APPROVED:
            if self.approved_action is None or self.quantity <= 0 or self.notional <= 0:
                raise ValueError("approved risk decision requires an executable plan")
            if self.leverage < 1 or self.required_margin <= 0:
                raise ValueError("approved risk decision requires leverage and margin")
        else:
'''
new = '''        if self.status is RiskDecisionStatus.APPROVED:
            if self.approved_action is None or self.quantity <= 0 or self.notional <= 0:
                raise ValueError("approved risk decision requires an executable plan")
            if self.leverage < 1:
                raise ValueError("approved risk decision requires valid leverage")
            opening = self.approved_action in {
                RiskApprovedAction.OPEN_LONG,
                RiskApprovedAction.OPEN_SHORT,
            }
            if opening:
                if self.reduce_only or self.required_margin <= 0 or self.stop_price is None:
                    raise ValueError("approved entry requires margin, stop and non-reduce-only plan")
            elif not self.reduce_only or self.required_margin != 0 or self.stop_price is not None:
                raise ValueError("approved exit must be reduce-only without new margin or stop")
        else:
'''

count = text.count(old)
if count != 1:
    raise SystemExit(f"expected exactly one approval contract block, found {count}")

path.write_text(text.replace(old, new), encoding="utf-8")
