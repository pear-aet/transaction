from pathlib import Path


path = Path("src/crypto_quant/research_engine.py")
text = path.read_text(encoding="utf-8")
old = '''    def _sync_cash(self) -> None:
        self.state.cash = float(self.account.balances.cash)
'''
new = '''    def _sync_cash(self) -> None:
        # Legacy EngineState.cash represents settled wallet assets, not free collateral.
        self.state.cash = float(self.account.balances.settled_assets)
'''
count = text.count(old)
if count == 1:
    path.write_text(text.replace(old, new), encoding="utf-8")
elif count == 0 and new in text:
    pass
else:
    raise SystemExit(f"expected one ResearchEventEngine cash projection, found {count}")
