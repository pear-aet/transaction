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
        f"expected one conservative-rounding source or existing target in {path}, "
        f"found {count}"
    )


def append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


CONTRACTS = Path("src/crypto_quant/portfolio_contracts.py")
ACCOUNTING = Path("src/crypto_quant/portfolio_accounting.py")
STORE = Path("src/crypto_quant/portfolio_store.py")
CONTRACTS_TEST = Path("tests/test_portfolio_contracts.py")

replace_once(
    CONTRACTS,
    '''from decimal import Decimal, InvalidOperation
''',
    '''from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
''',
)
replace_once(
    CONTRACTS,
    '''    def quantize_money(self, value: Decimal | int | float | str) -> Decimal:
        return as_decimal(value, "money").quantize(self.settlement_quantum)
''',
    '''    def quantize_money(self, value: Decimal | int | float | str) -> Decimal:
        return as_decimal(value, "money").quantize(self.settlement_quantum)

    def quantize_up(self, value: Decimal | int | float | str) -> Decimal:
        selected = as_decimal(value, "money")
        if selected < ZERO:
            raise ValueError("conservative upward quantization requires non-negative money")
        return selected.quantize(self.settlement_quantum, rounding=ROUND_CEILING)

    def quantize_down(self, value: Decimal | int | float | str) -> Decimal:
        selected = as_decimal(value, "money")
        if selected < ZERO:
            raise ValueError("conservative downward quantization requires non-negative money")
        return selected.quantize(self.settlement_quantum, rounding=ROUND_FLOOR)
''',
)

replace_once(
    ACCOUNTING,
    '''    policy_fee_reserve = policy.quantize_money(
        command_quantity * approval_price * policy.fee_reserve_rate
    )
''',
    '''    policy_fee_reserve = policy.quantize_up(
        command_quantity * approval_price * policy.fee_reserve_rate
    )
''',
)
replace_once(
    ACCOUNTING,
    '''        monetary_margin = policy.quantize_money(
            approved_notional / approved_leverage
        )
        approved_required_margin = policy.quantize_money(command.required_margin)
''',
    '''        monetary_margin = policy.quantize_up(
            approved_notional / approved_leverage
        )
        approved_required_margin = policy.quantize_up(command.required_margin)
''',
)
replace_once(
    ACCOUNTING,
    '''    required_margin_after = policy.quantize_money(
        next_cursor.cumulative_quote_value / approved_leverage
    )
''',
    '''    required_margin_after = policy.quantize_up(
        next_cursor.cumulative_quote_value / approved_leverage
    )
''',
)

accounting = ACCOUNTING.read_text(encoding="utf-8")
function_start = accounting.index("def apply_close_fill(")
block_start = accounting.index("    delta, next_cursor = derive_fill_delta(\n", function_start)
postings_start = accounting.index("    postings = [\n", block_start)
function_end = accounting.index("\n\ndef release_command_reservation(", postings_start)

new_prefix = '''    delta, next_cursor = derive_fill_delta(
        cursor=cursor,
        order=order,
        update=update,
        policy=policy,
    )
    quantity_tolerance = Decimal("0.000000000001")
    quantity_difference = delta.quantity - position.quantity
    if quantity_difference > quantity_tolerance:
        raise PortfolioAccountingError("reduce-only fill exceeds the open position")
    full_close = abs(quantity_difference) <= quantity_tolerance
    accounted_quantity = position.quantity if full_close else delta.quantity
    if accounted_quantity <= ZERO:
        raise PortfolioAccountingError("close fill quantity must be positive")
    expected_action = (
        RiskApprovedAction.CLOSE_LONG
        if position.side is PositionSide.LONG
        else RiskApprovedAction.CLOSE_SHORT
    )
    if command.action not in {
        expected_action,
        RiskApprovedAction.OPEN_LONG,
        RiskApprovedAction.OPEN_SHORT,
    }:
        raise PortfolioAccountingError("close fill command targets another position")
    if reservation.entry:
        if command.command_id != position.open_command_id:
            raise PortfolioAccountingError("protective fill belongs to another open command")
        if order.leg_type is not OrderLegType.PROTECTIVE_STOP:
            raise PortfolioAccountingError("entry command may close only through protection")
    else:
        if reservation.action is not expected_action:
            raise PortfolioAccountingError("exit reservation targets another position")
        if accounted_quantity - reservation.reserved_exit_quantity > quantity_tolerance:
            raise PortfolioAccountingError("exit fill exceeds its quantity lock")

    if full_close:
        margin_release = position.position_margin
        risk_release = position.initial_risk
    else:
        margin_release = policy.quantize_down(
            position.position_margin * accounted_quantity / position.quantity
        )
        risk_release = position.initial_risk * accounted_quantity / position.quantity
    sign = Decimal("1") if position.side is PositionSide.LONG else Decimal("-1")
    realized = policy.quantize_money(
        sign * accounted_quantity * (delta.price - position.average_entry_price)
    )

'''

tail = accounting[postings_start:function_end].replace(
    "delta.quantity",
    "accounted_quantity",
)
accounting = accounting[:block_start] + new_prefix + tail + accounting[function_end:]
ACCOUNTING.write_text(accounting, encoding="utf-8")

store = STORE.read_text(encoding="utf-8")
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
                connection.execute(
                    """UPDATE account_instances SET
                    status='halted',halt_reason=?,
                    updated_at=max(updated_at,initialized_at,?)
                    WHERE account_id=?""",
                    (
                        f"reconciliation:{finding.kind.value}:{finding.finding_id}",
                        finding.found_at,
                        finding.account_id,
                    ),
                )
'''
count = store.count(old)
if count == 1:
    STORE.write_text(store.replace(old, new), encoding="utf-8")
elif count == 0 and new in store:
    pass
else:
    raise SystemExit(
        f"expected one critical finding source or existing target, found {count}"
    )

append_once(
    CONTRACTS_TEST,
    "def test_policy_conservative_money_quantization(",
    '''def test_policy_conservative_money_quantization() -> None:
    policy = AccountPolicy(settlement_quantum=Decimal("0.01"))

    assert policy.quantize_money(Decimal("1.005")) == Decimal("1.00")
    assert policy.quantize_up(Decimal("1.001")) == Decimal("1.01")
    assert policy.quantize_up(Decimal("1.010")) == Decimal("1.01")
    assert policy.quantize_down(Decimal("1.009")) == Decimal("1.00")
    assert policy.quantize_down(Decimal("1.010")) == Decimal("1.01")

    with pytest.raises(ValueError, match="non-negative"):
        policy.quantize_up(Decimal("-0.001"))
    with pytest.raises(ValueError, match="non-negative"):
        policy.quantize_down(Decimal("-0.001"))
''',
)
