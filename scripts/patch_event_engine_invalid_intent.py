from pathlib import Path

path = Path("src/crypto_quant/engine.py")
text = path.read_text(encoding="utf-8")
old = '''    def _open(self, symbol: str, signal: Signal, bar: Candle) -> None:
        decision = self._entry_risk_decision(symbol, signal, bar)
        if decision.status is RiskDecisionStatus.REJECTED:
'''
new = '''    def _open(self, symbol: str, signal: Signal, bar: Candle) -> None:
        try:
            decision = self._entry_risk_decision(symbol, signal, bar)
        except ValueError as exc:
            self._record(
                bar.timestamp,
                "entry_rejected",
                symbol=symbol,
                reason="invalid_strategy_intent",
                error=str(exc),
            )
            return
        if decision.status is RiskDecisionStatus.REJECTED:
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one EventEngine open block, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
