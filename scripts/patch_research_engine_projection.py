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
        f"expected one ResearchEventEngine projection source or target, found {count}"
    )


path = Path("src/crypto_quant/research_engine.py")
replace_once(
    path,
    '''    def _sync_cash(self) -> None:
        self.state.cash = float(self.account.balances.cash)
''',
    '''    def _sync_cash(self) -> None:
        # EngineState defers funding settlement until close. The authoritative
        # account settles at each funding event, so add the open funding payable
        # back only for the compatibility projection.
        deferred_funding = sum(
            (Decimal(str(value)) for value in self.state.funding_accrued.values()),
            Decimal("0"),
        )
        self.state.cash = float(
            self.account.balances.settled_assets + deferred_funding
        )
''',
)
replace_once(
    path,
    '''    def _sync_cash(self) -> None:
        # Legacy EngineState.cash represents settled wallet assets, not free collateral.
        self.state.cash = float(self.account.balances.settled_assets)
''',
    '''    def _sync_cash(self) -> None:
        # EngineState defers funding settlement until close. The authoritative
        # account settles at each funding event, so add the open funding payable
        # back only for the compatibility projection.
        deferred_funding = sum(
            (Decimal(str(value)) for value in self.state.funding_accrued.values()),
            Decimal("0"),
        )
        self.state.cash = float(
            self.account.balances.settled_assets + deferred_funding
        )
''',
)
replace_once(
    path,
    '''        self._sync_cash()
        self._sync_position(symbol)
        if effective > 10 and timeframe_ms(self.config.low_timeframe) > 60_000:
''',
    '''        self._sync_position(symbol)
        self._sync_cash()
        if effective > 10 and timeframe_ms(self.config.low_timeframe) > 60_000:
''',
)
replace_once(
    path,
    '''        self._sync_cash()
        self._sync_position(symbol)
        self._targets.pop(symbol, None)
''',
    '''        self._sync_position(symbol)
        self._sync_cash()
        self._targets.pop(symbol, None)
''',
)
replace_once(
    path,
    '''                self._sync_cash()
                self._sync_position(symbol)
                applied.add(rate.timestamp)
''',
    '''                self._sync_position(symbol)
                self._sync_cash()
                applied.add(rate.timestamp)
''',
)
