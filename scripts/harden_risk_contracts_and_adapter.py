from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/crypto_quant/engine.py",
    '''    def _entry_risk_decision(
        self,
        symbol: str,
        signal: Signal,
        bar: Candle,
    ) -> RiskDecision:
        return self.risk_evaluator(
            RiskContext(
                "event-engine-risk-v1",
                self._entry_risk_intent(symbol, signal, bar),
                self._entry_account_snapshot(symbol, bar),
''',
    '''    def _entry_risk_decision(
        self,
        symbol: str,
        intent: StrategyIntent,
        bar: Candle,
    ) -> RiskDecision:
        return self.risk_evaluator(
            RiskContext(
                "event-engine-risk-v1",
                intent,
                self._entry_account_snapshot(symbol, bar),
''',
)
replace_once(
    "src/crypto_quant/engine.py",
    '''    def _open(self, symbol: str, signal: Signal, bar: Candle) -> None:
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
''',
    '''    def _open(self, symbol: str, signal: Signal, bar: Candle) -> None:
        try:
            intent = self._entry_risk_intent(symbol, signal, bar)
        except ValueError as exc:
            self._record(
                bar.timestamp,
                "entry_rejected",
                symbol=symbol,
                reason="invalid_strategy_intent",
                error=str(exc),
            )
            return
        decision = self._entry_risk_decision(symbol, intent, bar)
        if decision.status is RiskDecisionStatus.REJECTED:
''',
)

replace_once(
    "src/crypto_quant/risk_contracts.py",
    '''        if not self.reasons:
            raise ValueError("risk decision requires at least one reason")
        numeric = (
''',
    '''        if not self.reasons:
            raise ValueError("risk decision requires at least one reason")
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError("risk decision reasons must be unique")
        numeric = (
''',
)
replace_once(
    "src/crypto_quant/risk_contracts.py",
    '''        if self.status is RiskDecisionStatus.APPROVED:
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
                    raise ValueError(
                        "approved entry requires margin, stop and non-reduce-only plan"
                    )
            elif not self.reduce_only or self.required_margin != 0 or self.stop_price is not None:
                raise ValueError("approved exit must be reduce-only without new margin or stop")
        else:
            if self.approved_action is not None or self.reduce_only:
                raise ValueError("rejected risk decision cannot contain an approved action")
            if any(value != 0 for value in (self.quantity, self.notional, self.leverage)):
                raise ValueError("rejected risk decision cannot contain order sizing")
''',
    '''        if self.status is RiskDecisionStatus.APPROVED:
            if self.approved_action is None or self.quantity <= 0 or self.notional <= 0:
                raise ValueError("approved risk decision requires an executable plan")
            if self.leverage < 1:
                raise ValueError("approved risk decision requires valid leverage")
            opening = self.approved_action in {
                RiskApprovedAction.OPEN_LONG,
                RiskApprovedAction.OPEN_SHORT,
            }
            if opening:
                expected = {
                    StrategyAction.ENTER_LONG: RiskApprovedAction.OPEN_LONG,
                    StrategyAction.ENTER_SHORT: RiskApprovedAction.OPEN_SHORT,
                }.get(self.requested_action)
                if expected is not self.approved_action:
                    raise ValueError("approved entry action does not match the strategy request")
                if RiskReason.APPROVED_ENTRY not in self.reasons:
                    raise ValueError("approved entry requires the approved_entry reason")
                if self.reduce_only or self.required_margin <= 0 or self.stop_price is None:
                    raise ValueError(
                        "approved entry requires margin, stop and non-reduce-only plan"
                    )
            else:
                if self.requested_action is not StrategyAction.EXIT:
                    raise ValueError("approved exit does not match the strategy request")
                if RiskReason.APPROVED_EXIT not in self.reasons:
                    raise ValueError("approved exit requires the approved_exit reason")
                if not self.reduce_only or self.required_margin != 0 or self.stop_price is not None:
                    raise ValueError(
                        "approved exit must be reduce-only without new margin or stop"
                    )
        else:
            if self.approved_action is not None or self.reduce_only:
                raise ValueError("rejected risk decision cannot contain an approved action")
            if RiskReason.APPROVED_ENTRY in self.reasons or RiskReason.APPROVED_EXIT in self.reasons:
                raise ValueError("rejected risk decision cannot contain an approval reason")
            execution_values = (
                self.quantity,
                self.notional,
                self.leverage,
                self.required_margin,
                self.risk_budget,
                self.initial_risk,
            )
            if any(value != 0 for value in execution_values):
                raise ValueError("rejected risk decision cannot contain execution sizing")
''',
)
