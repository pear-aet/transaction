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
        f"expected one lifecycle-hardening source or existing target in {path}, "
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
ACCOUNTING_TEST = Path("tests/test_portfolio_accounting.py")

replace_once(
    CONTRACTS,
    '''from decimal import Decimal, InvalidOperation
''',
    '''from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
''',
)
replace_once(
    CONTRACTS,
    '''    last_funding_time: int | None = None
''',
    '''    last_funding_time: int | None = None
    last_execution_time: int | None = None
    last_mark_time: int | None = None
''',
)
replace_once(
    CONTRACTS,
    '''        if self.last_funding_time is not None:
            if self.last_funding_time < self.opened_at:
                raise ValueError("position funding time precedes opening")
            if self.last_funding_time > self.updated_at:
                raise ValueError("position funding time exceeds updated_at")
        if not ZERO < self.maintenance_margin_rate < Decimal("1"):
''',
    '''        execution_time = (
            self.opened_at
            if self.last_execution_time is None
            else self.last_execution_time
        )
        object.__setattr__(self, "last_execution_time", execution_time)
        for name, value in (
            ("last_execution_time", execution_time),
            ("last_mark_time", self.last_mark_time),
            ("last_funding_time", self.last_funding_time),
        ):
            if value is None:
                continue
            if value < self.opened_at:
                raise ValueError(f"position {name} precedes opening")
            if value > self.updated_at:
                raise ValueError(f"position {name} exceeds updated_at")
        if not ZERO < self.maintenance_margin_rate < Decimal("1"):
''',
)
replace_once(
    CONTRACTS,
    '''            "last_funding_time": self.last_funding_time,
        }
''',
    '''            "last_funding_time": self.last_funding_time,
            "last_execution_time": self.last_execution_time,
            "last_mark_time": self.last_mark_time,
        }
''',
)
replace_once(
    CONTRACTS,
    '''            last_funding_time=(
                None
                if value.get("last_funding_time") is None
                else int(value["last_funding_time"])
            ),
        )
''',
    '''            last_funding_time=(
                None
                if value.get("last_funding_time") is None
                else int(value["last_funding_time"])
            ),
            last_execution_time=(
                None
                if value.get("last_execution_time") is None
                else int(value["last_execution_time"])
            ),
            last_mark_time=(
                None
                if value.get("last_mark_time") is None
                else int(value["last_mark_time"])
            ),
        )
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
replace_once(
    ACCOUNTING,
    '''    if position is not None:
        if position.side is not expected_side or position.open_command_id != command.command_id:
            raise PortfolioAccountingError("open fill conflicts with the existing position")
''',
    '''    if position is not None:
        if position.side is not expected_side or position.open_command_id != command.command_id:
            raise PortfolioAccountingError("open fill conflicts with the existing position")
        assert position.last_execution_time is not None
        if update.event_time < position.last_execution_time:
            raise PortfolioAccountingError("open fill execution time moved backwards")
        if (
            position.last_funding_time is not None
            and update.event_time <= position.last_funding_time
        ):
            raise PortfolioAccountingError(
                "late open fill crosses an applied funding boundary"
            )
''',
)
replace_once(
    ACCOUNTING,
    '''            opened_at=update.event_time,
            updated_at=update.event_time,
            opening_fees=delta.fee,
''',
    '''            opened_at=update.event_time,
            updated_at=update.event_time,
            opening_fees=delta.fee,
            last_execution_time=update.event_time,
''',
)
replace_once(
    ACCOUNTING,
    '''            mark_price=delta.price,
            position_margin=position.position_margin + margin_allocation,
            initial_risk=initial_risk_after,
            maintenance_margin_rate=margin_rate,
            opening_fees=position.opening_fees + delta.fee,
            updated_at=update.event_time,
            version=position.version + 1,
''',
    '''            mark_price=(
                position.mark_price
                if position.last_mark_time is not None
                and position.last_mark_time >= update.event_time
                else delta.price
            ),
            position_margin=position.position_margin + margin_allocation,
            initial_risk=initial_risk_after,
            maintenance_margin_rate=margin_rate,
            opening_fees=position.opening_fees + delta.fee,
            last_execution_time=update.event_time,
            updated_at=max(position.updated_at, update.event_time),
            version=position.version + 1,
''',
)
replace_once(
    ACCOUNTING,
    '''def apply_mark_price(
    position: PortfolioPosition,
    *,
    mark_price: Decimal | int | float | str,
    observed_at: int,
) -> PortfolioPosition:
    selected = as_decimal(mark_price, "mark_price")
    if selected <= ZERO:
        raise ValueError("mark_price must be positive")
    if observed_at < position.updated_at:
        raise PortfolioAccountingError("mark price time moved backwards")
    if observed_at == position.updated_at:
        if selected != position.mark_price:
            raise PortfolioAccountingError("same-time mark price revision")
        return position
    return replace(
        position,
        mark_price=selected,
        updated_at=observed_at,
        version=position.version + 1,
    )
''',
    '''def apply_mark_price(
    position: PortfolioPosition,
    *,
    mark_price: Decimal | int | float | str,
    observed_at: int,
) -> PortfolioPosition:
    selected = as_decimal(mark_price, "mark_price")
    if selected <= ZERO:
        raise ValueError("mark_price must be positive")
    assert position.last_execution_time is not None
    if observed_at < position.last_execution_time:
        raise PortfolioAccountingError("mark price precedes the latest execution")
    if position.last_mark_time is not None:
        if observed_at < position.last_mark_time:
            raise PortfolioAccountingError("mark price time moved backwards")
        if observed_at == position.last_mark_time:
            if selected != position.mark_price:
                raise PortfolioAccountingError("same-time mark price revision")
            return position
    return replace(
        position,
        mark_price=selected,
        last_mark_time=observed_at,
        updated_at=max(position.updated_at, observed_at),
        version=position.version + 1,
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
    assert position.last_execution_time is not None
    if update.event_time < position.last_execution_time:
        raise PortfolioAccountingError("close fill execution time moved backwards")
    if (
        position.last_funding_time is not None
        and update.event_time <= position.last_funding_time
    ):
        raise PortfolioAccountingError(
            "late close fill crosses an applied funding boundary"
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
tail = tail.replace(
    '''            mark_price=delta.price,
            position_margin=position.position_margin - margin_release,
            initial_risk=position.initial_risk - risk_release,
            updated_at=update.event_time,
''',
    '''            mark_price=(
                position.mark_price
                if position.last_mark_time is not None
                and position.last_mark_time >= update.event_time
                else delta.price
            ),
            position_margin=position.position_margin - margin_release,
            initial_risk=position.initial_risk - risk_release,
            last_execution_time=update.event_time,
            updated_at=max(position.updated_at, update.event_time),
''',
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


def test_position_time_axes_round_trip_and_validate() -> None:
    position = PortfolioPosition(
        position_id="time-axis-position",
        account_id="paper",
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        average_entry_price=Decimal("100"),
        mark_price=Decimal("101"),
        stop_price=Decimal("99"),
        position_margin=Decimal("10"),
        initial_risk=Decimal("1"),
        maintenance_margin_rate=Decimal("0.005"),
        correlation_group="default",
        open_command_id="time-axis-command",
        open_risk_decision_id="time-axis-risk",
        open_intent_id="time-axis-intent",
        opened_at=10,
        updated_at=30,
        last_execution_time=20,
        last_mark_time=30,
    )
    assert position.last_execution_time == 20
    assert position.last_mark_time == 30
    assert PortfolioPosition.from_dict(position.to_dict()) == position

    with pytest.raises(ValueError, match="last_execution_time exceeds"):
        replace(position, last_execution_time=31)
''',
)

append_once(
    ACCOUNTING_TEST,
    "def test_late_partial_fill_preserves_newer_canonical_mark(",
    '''def test_late_partial_fill_preserves_newer_canonical_mark() -> None:
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
    assert plan.transaction is not None
    balances = balances.apply(plan.transaction)
    primary = next(order for order in command.orders if order.leg_type is OrderLegType.PRIMARY)
    quantity = Decimal(str(command.quantity))
    first_quantity = quantity / Decimal("2")
    first_quote = first_quantity * Decimal("100")
    first_fee = policy.quantize_money(first_quote * Decimal("0.001"))
    first = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="time-axis-open-1",
            cumulative_quantity=first_quantity,
            cumulative_quote_value=first_quote,
            cumulative_fee=first_fee,
            event_time=NOW + 1,
            state=OrderState.PARTIALLY_FILLED,
        ),
        cursor=OrderAccountingCursor(primary.order_id, primary.command_id),
        reservation=plan.reservation,
        balances=balances,
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
    )
    assert first.position is not None
    marked = apply_mark_price(first.position, mark_price=105, observed_at=NOW + 3)

    total_quote = first_quote + (quantity - first_quantity) * Decimal("101")
    total_fee = policy.quantize_money(total_quote * Decimal("0.001"))
    second = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="time-axis-open-2",
            cumulative_quantity=quantity,
            cumulative_quote_value=total_quote,
            cumulative_fee=total_fee,
            event_time=NOW + 2,
            state=OrderState.FILLED,
        ),
        cursor=first.cursor,
        reservation=first.reservation,
        balances=balances.apply(first.transaction),
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
        position=marked,
    )
    assert second.position is not None
    assert second.position.last_execution_time == NOW + 2
    assert second.position.last_mark_time == NOW + 3
    assert second.position.updated_at == NOW + 3
    assert second.position.mark_price == Decimal("105")

    with pytest.raises(PortfolioAccountingError, match="latest execution"):
        apply_mark_price(second.position, mark_price=104, observed_at=NOW + 1)
''',
)
