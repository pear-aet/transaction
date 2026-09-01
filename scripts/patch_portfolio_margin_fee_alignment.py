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
        f"expected one margin-fee source or existing target in {path}, found {count}"
    )


def append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


ACCOUNTING = Path("src/crypto_quant/portfolio_accounting.py")
TEST = Path("tests/test_portfolio_accounting.py")

replace_once(
    ACCOUNTING,
    '''    if command.created_at > now_ms:
        raise PortfolioAccountingError("command reservation time precedes command creation")
    if command.mode is ExecutionMode.SHADOW:
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
''',
    '''    if command.created_at > now_ms:
        raise PortfolioAccountingError("command reservation time precedes command creation")
    entry = command.action in {
        RiskApprovedAction.OPEN_LONG,
        RiskApprovedAction.OPEN_SHORT,
    }
    if command.mode is ExecutionMode.SHADOW:
        state = ReservationState.SHADOW_RECORDED
        monetary_margin = ZERO
        fee_reserve = ZERO
    elif entry:
        state = ReservationState.ACTIVE
        decision_notional = as_decimal(decision.notional, "risk decision notional")
        decision_leverage = as_decimal(decision.leverage, "risk decision leverage")
        if decision_leverage <= ZERO:
            raise PortfolioAccountingError("entry risk decision has non-positive leverage")
        monetary_margin = policy.quantize_money(decision_notional / decision_leverage)
        approved_total = policy.quantize_money(command.required_margin)
        if approved_total < monetary_margin:
            raise PortfolioAccountingError(
                "risk required margin is below its initial-margin component"
            )
        # Risk required_margin already includes the approved entry fee. Keep
        # that fee in the fee bucket instead of misclassifying it as position
        # margin. Do not reserve an additional round-trip fee here: that would
        # make a Risk-approved order fail Portfolio solely because of duplicate
        # capital reservation.
        fee_reserve = approved_total - monetary_margin
    else:
        state = ReservationState.ACTIVE
        monetary_margin = ZERO
        # Risk-reducing exits must never be blocked because free cash cannot be
        # pre-reserved for their fee. The close transaction releases position
        # margin and settles the actual fee atomically.
        fee_reserve = ZERO
''',
)

append_once(
    TEST,
    "def test_entry_reservation_does_not_double_count_risk_approved_fee(",
    '''def test_entry_reservation_does_not_double_count_risk_approved_fee() -> None:
    policy = AccountPolicy()
    decision = risk_decision()
    command = execution_command(decision)
    approved_total = policy.quantize_money(command.required_margin)
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

    expected_margin = policy.quantize_money(
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
