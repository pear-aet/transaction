from pathlib import Path


ACCOUNTING = Path("src/crypto_quant/portfolio_accounting.py")
STORE = Path("src/crypto_quant/portfolio_store.py")

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
        if (
            accounted_quantity - reservation.reserved_exit_quantity
            > quantity_tolerance
        ):
            raise PortfolioAccountingError("exit fill exceeds its quantity lock")

    if full_close:
        margin_release = position.position_margin
        risk_release = position.initial_risk
    else:
        margin_release = policy.quantize_money(
            position.position_margin * accounted_quantity / position.quantity
        )
        risk_release = (
            position.initial_risk * accounted_quantity / position.quantity
        )
    sign = Decimal("1") if position.side is PositionSide.LONG else Decimal("-1")
    realized = policy.quantize_money(
        sign
        * accounted_quantity
        * (delta.price - position.average_entry_price)
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
if count != 1:
    raise SystemExit(f"expected one critical finding halt block, found {count}")
STORE.write_text(store.replace(old, new), encoding="utf-8")
