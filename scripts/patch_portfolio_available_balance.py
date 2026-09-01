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
        f"expected one available-balance source or existing target in {path}, found {count}"
    )


def append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


CONTRACTS = Path("src/crypto_quant/portfolio_contracts.py")
TEST = Path("tests/test_portfolio_contracts.py")

replace_once(
    CONTRACTS,
    '''    @property
    def available_balance(self) -> Decimal:
        return self.balances.cash
''',
    '''    @property
    def available_balance(self) -> Decimal:
        # ``cash`` is the free settled balance after command reserves and
        # position margin have already been carved out of settled assets.
        # In cross margin, unrealized PnL changes the collateral that can back
        # another order, so free collateral must move with the mark as well.
        return self.balances.cash + self.unrealized_pnl
''',
)

append_once(
    TEST,
    "def test_available_balance_includes_cross_margin_unrealized_pnl(",
    '''def test_available_balance_includes_cross_margin_unrealized_pnl() -> None:
    balances = AccountBalances(
        cash=Decimal("900"),
        position_margin=Decimal("100"),
        external_capital=Decimal("1000"),
    )
    position = PortfolioPosition(
        position_id="available-balance-position",
        account_id="paper",
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        quantity=Decimal("2"),
        average_entry_price=Decimal("100"),
        mark_price=Decimal("90"),
        stop_price=Decimal("80"),
        position_margin=Decimal("100"),
        initial_risk=Decimal("40"),
        maintenance_margin_rate=Decimal("0.005"),
        correlation_group="default",
        open_command_id="available-balance-command",
        open_risk_decision_id="available-balance-risk",
        open_intent_id="available-balance-intent",
        opened_at=10,
        updated_at=20,
    )
    loss_snapshot = build_account_snapshot(
        account_id="paper",
        observed_at=20,
        balances=balances,
        positions=(position,),
        day_start_equity=Decimal("1000"),
        peak_equity=Decimal("1000"),
        consecutive_losses=0,
        watermarks=AccountWatermarks(),
    )

    assert loss_snapshot.unrealized_pnl == Decimal("-20")
    assert loss_snapshot.equity == Decimal("980")
    assert loss_snapshot.available_balance == Decimal("880")
    assert loss_snapshot.to_risk_snapshot().available_balance == 880.0

    profit_position = replace(position, mark_price=Decimal("110"))
    profit_snapshot = build_account_snapshot(
        account_id="paper",
        observed_at=20,
        balances=balances,
        positions=(profit_position,),
        day_start_equity=Decimal("1000"),
        peak_equity=Decimal("1020"),
        consecutive_losses=0,
        watermarks=AccountWatermarks(),
    )
    assert profit_snapshot.unrealized_pnl == Decimal("20")
    assert profit_snapshot.equity == Decimal("1020")
    assert profit_snapshot.available_balance == Decimal("920")
    assert profit_snapshot.to_risk_snapshot().available_balance == 920.0
''',
)
