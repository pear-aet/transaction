from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 1:
        path.write_text(text.replace(old, new), encoding="utf-8")
        return
    if count == 0 and new in text:
        return
    raise SystemExit(
        f"Portfolio margin-fee patch {label} expected one source or existing target, "
        f"found source={count} target={text.count(new)}"
    )


def append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


ACCOUNTING = Path("src/crypto_quant/portfolio_accounting.py")
TEST = Path("tests/test_portfolio_accounting.py")

# lifecycle_failures.py already separates initial margin from a fee bucket and
# lifecycle_round4.py makes reservation quantization conservative. Upgrade that
# intermediate representation instead of reapplying the original reservation
# patch against a source shape that no longer exists.
replace_once(
    ACCOUNTING,
    '''    command_quantity = as_decimal(command.quantity, "command quantity")
    approval_price = as_decimal(command.approval_price, "approval price")
    policy_fee_reserve = policy.quantize_up(
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
        monetary_margin = policy.quantize_up(
            approved_notional / approved_leverage
        )
        approved_required_margin = policy.quantize_up(command.required_margin)
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
    '''    command_quantity = as_decimal(command.quantity, "command quantity")
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
        monetary_margin = policy.quantize_up(
            approved_notional / approved_leverage
        )
        approved_required_margin = policy.quantize_up(command.required_margin)
        if approved_required_margin < monetary_margin:
            raise PortfolioAccountingError(
                "risk required margin is below its initial-margin component"
            )
        # Risk required_margin already includes its approved entry fee. Keep
        # exactly that allowance in the fee bucket; reserving policy_fee_rate
        # again would make a Risk-approved command fail Portfolio because the
        # same fee capital was counted twice.
        fee_reserve = approved_required_margin - monetary_margin
    else:
        state = ReservationState.ACTIVE
        monetary_margin = ZERO
        # Risk-reducing exits must never be blocked by a fee pre-reservation.
        # The close transaction releases position margin and settles the actual
        # fee atomically from the resulting account balances.
        fee_reserve = ZERO
''',
    "reservation_policy",
)

# Replace the older lifecycle acceptance assertion which explicitly required an
# additional policy fee buffer. That behavior is the double-reservation bug this
# hardening stage removes.
replace_once(
    TEST,
    '''    approved_fee_allowance = approved_required_margin - expected_initial_margin
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
''',
    '''    approved_fee_allowance = approved_required_margin - expected_initial_margin

    assert plan.reservation.reserved_margin_total == expected_initial_margin
    assert plan.reservation.command_margin_remaining == expected_initial_margin
    assert plan.reservation.fee_reserve_total == approved_fee_allowance
    assert (
        plan.reservation.reserved_margin_total
        + plan.reservation.fee_reserve_total
        == approved_required_margin
    )
''',
    "legacy_acceptance_assertion",
)

append_once(
    TEST,
    "def test_entry_reservation_does_not_double_count_risk_approved_fee(",
    '''def test_entry_reservation_does_not_double_count_risk_approved_fee() -> None:
    policy = AccountPolicy()
    decision = risk_decision()
    command = execution_command(decision)
    approved_total = policy.quantize_up(command.required_margin)
    balances = AccountBalances(
        cash=approved_total,
        external_capital=approved_total,
    )

    plan = plan_command_reservation(
        account_id="paper",
        command=command,
        decision=decision,
        balances=balances,
        policy=policy,
        now_ms=NOW,
    )

    expected_margin = policy.quantize_up(
        Decimal(str(decision.notional)) / Decimal(str(decision.leverage))
    )
    assert plan.reservation.reserved_margin_total == expected_margin
    assert plan.reservation.fee_reserve_total == approved_total - expected_margin
    assert (
        plan.reservation.reserved_margin_total + plan.reservation.fee_reserve_total
        == approved_total
    )
    assert plan.transaction is not None
    after = balances.apply(plan.transaction)
    assert after.cash == 0
    assert after.command_margin == expected_margin
    assert after.command_fee_reserve == approved_total - expected_margin


def test_exit_reservation_never_requires_free_cash_for_fee() -> None:
    policy = AccountPolicy()
    decision = risk_decision(StrategyAction.EXIT, storage_sequence=2)
    command = execution_command(decision)
    position = PortfolioPosition(
        position_id="cashless-exit-position",
        account_id="paper",
        symbol=command.symbol,
        side=PositionSide.LONG,
        quantity=Decimal(str(command.quantity)),
        average_entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        stop_price=Decimal("90"),
        position_margin=Decimal("100"),
        initial_risk=Decimal("20"),
        maintenance_margin_rate=Decimal("0.005"),
        correlation_group="default",
        open_command_id="cashless-open-command",
        open_risk_decision_id="cashless-open-risk",
        open_intent_id="cashless-open-intent",
        opened_at=NOW - 10,
        updated_at=NOW,
    )
    balances = AccountBalances(
        cash=Decimal("0"),
        position_margin=Decimal("100"),
        external_capital=Decimal("100"),
    )

    plan = plan_command_reservation(
        account_id="paper",
        command=command,
        decision=decision,
        balances=balances,
        policy=policy,
        now_ms=NOW,
        current_position=position,
    )

    assert plan.reservation.reserved_margin_total == 0
    assert plan.reservation.fee_reserve_total == 0
    assert plan.reservation.reserved_exit_quantity == Decimal(str(command.quantity))
    assert plan.transaction is None
''',
)
