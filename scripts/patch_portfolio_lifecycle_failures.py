from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 1:
        file.write_text(text.replace(old, new), encoding="utf-8")
        return
    if count == 0 and new in text:
        return
    raise SystemExit(f"expected one source or existing target in {path}, found {count}")


def append_once(path: str, marker: str, content: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if marker in text:
        return
    file.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


replace_once(
    "src/crypto_quant/portfolio_accounting.py",
    '''    if command.mode is ExecutionMode.SHADOW:
        state = ReservationState.SHADOW_RECORDED
        monetary_margin = ZERO
        fee_reserve = ZERO
    else:
        state = ReservationState.ACTIVE
        monetary_margin = policy.quantize_money(command.required_margin)
        fee_reserve = policy.quantize_money(
            as_decimal(command.quantity, "command quantity")
            * as_decimal(command.approval_price, "approval price")
            * policy.fee_reserve_rate
        )

    entry = command.action in {
        RiskApprovedAction.OPEN_LONG,
        RiskApprovedAction.OPEN_SHORT,
    }
    command_quantity = as_decimal(command.quantity, "command quantity")
''',
    '''    entry = command.action in {
        RiskApprovedAction.OPEN_LONG,
        RiskApprovedAction.OPEN_SHORT,
    }
    command_quantity = as_decimal(command.quantity, "command quantity")
    approval_price = as_decimal(command.approval_price, "approval price")
    policy_fee_reserve = policy.quantize_money(
        command_quantity * approval_price * policy.fee_reserve_rate
    )
    if command.mode is ExecutionMode.SHADOW:
        state = ReservationState.SHADOW_RECORDED
        monetary_margin = ZERO
        fee_reserve = ZERO
    elif entry:
        state = ReservationState.ACTIVE
        approved_notional = as_decimal(decision.notional, "approved notional")
        approved_leverage = as_decimal(decision.leverage, "approved leverage")
        if approved_notional <= ZERO or approved_leverage < Decimal("1"):
            raise PortfolioAccountingError(
                "approved entry has invalid notional or leverage"
            )
        monetary_margin = policy.quantize_money(
            approved_notional / approved_leverage
        )
        approved_required_margin = policy.quantize_money(command.required_margin)
        if approved_required_margin < monetary_margin:
            raise PortfolioAccountingError(
                "approved required margin is below initial margin"
            )
        approved_fee_allowance = approved_required_margin - monetary_margin
        fee_reserve = max(policy_fee_reserve, approved_fee_allowance)
    else:
        state = ReservationState.ACTIVE
        monetary_margin = ZERO
        fee_reserve = policy_fee_reserve
''',
)

replace_once(
    "src/crypto_quant/portfolio_accounting.py",
    '''        if closed_quantity != total_quantity:
            raise PortfolioAccountingError("closed quantity does not match opened quantity")
        closed_position = ClosedPosition(
''',
    '''        quantity_difference = abs(closed_quantity - total_quantity)
        if quantity_difference > Decimal("0.000000000001"):
            raise PortfolioAccountingError("closed quantity does not match opened quantity")
        closed_quantity = total_quantity
        closed_position = ClosedPosition(
''',
)

replace_once(
    "tests/test_portfolio_lifecycle.py",
    '''    with pytest.raises(ValueError, match="ID does not match"):
''',
    '''    with pytest.raises(ValueError, match="mark fact ID"):
''',
)

path = Path("tests/test_portfolio_lifecycle_store.py")
text = path.read_text(encoding="utf-8")
old_import = '''from crypto_quant.portfolio_accounting import (
    OrderAccountingCursor,
'''
new_import = '''from crypto_quant.portfolio_accounting import (
    MarkAccountingResult,
    OrderAccountingCursor,
'''
if old_import in text:
    text = text.replace(old_import, new_import)
elif new_import not in text:
    raise SystemExit("Portfolio lifecycle store test import anchor is missing")
old = '''    with pytest.raises(AccountStoreConflict, match="moved backwards|revision"):
        store.commit_mark(apply_mark_fact(first_funding.position, revision), policy)
'''
new = '''    with pytest.raises(AccountStoreConflict, match="moved backwards|revision"):
        store.commit_mark(MarkAccountingResult(revision, None), policy)
'''
count = text.count(old)
if count == 1:
    text = text.replace(old, new)
elif count == 0 and new in text:
    pass
else:
    raise SystemExit(
        f"expected one mark revision source or existing target, found {count}"
    )
path.write_text(text, encoding="utf-8")

append_once(
    "tests/test_portfolio_accounting.py",
    "def test_reservation_separates_initial_margin_from_fee_buffer(",
    '''def test_reservation_separates_initial_margin_from_fee_buffer() -> None:
    policy = AccountPolicy()
    balances = initialized_balances(policy)
    decision = risk_decision()
    command = execution_command(decision)

    plan = plan_command_reservation(
        account_id="paper",
        command=command,
        decision=decision,
        balances=balances,
        policy=policy,
        now_ms=command.created_at,
    )
    expected_initial_margin = policy.quantize_money(
        Decimal(str(decision.notional)) / Decimal(str(decision.leverage))
    )
    approved_required_margin = policy.quantize_money(command.required_margin)
    approved_fee_allowance = approved_required_margin - expected_initial_margin
    policy_fee_allowance = policy.quantize_money(
        Decimal(str(command.quantity))
        * Decimal(str(command.approval_price))
        * policy.fee_reserve_rate
    )

    assert plan.reservation.reserved_margin_total == expected_initial_margin
    assert plan.reservation.command_margin_remaining == expected_initial_margin
    assert plan.reservation.fee_reserve_total == max(
        approved_fee_allowance,
        policy_fee_allowance,
    )
    assert (
        plan.reservation.reserved_margin_total
        + plan.reservation.fee_reserve_total
        >= approved_required_margin
    )

    assert plan.transaction is not None
    reserved_balances = balances.apply(plan.transaction)
    primary = next(
        order for order in command.orders if order.leg_type is OrderLegType.PRIMARY
    )
    quantity = Decimal(str(command.quantity))
    quote = quantity * Decimal(str(command.approval_price))
    fee = policy.quantize_money(quote * Decimal("0.001"))
    opened = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="margin-fee-separation-fill",
            cumulative_quantity=quantity,
            cumulative_quote_value=quote,
            cumulative_fee=fee,
            event_time=command.created_at + 1,
            state=OrderState.FILLED,
        ),
        cursor=OrderAccountingCursor(primary.order_id, primary.command_id),
        reservation=plan.reservation,
        balances=reserved_balances,
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
    )

    assert opened.position is not None
    assert opened.position.position_margin == expected_initial_margin
    assert opened.reservation.command_margin_remaining == Decimal("0")
    assert opened.reservation.fee_reserve_remaining == (
        plan.reservation.fee_reserve_total - fee
    )
''',
)
