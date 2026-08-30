from pathlib import Path

path = Path("scripts/patch_portfolio_lifecycle.py")
text = path.read_text(encoding="utf-8")
replacements = {
    'file.write_text(text.rstrip() + "\\n\\n" + content.lstrip(), encoding="utf-8")': (
        'file.write_text(text.rstrip() + "\\n\\n" + content.lstrip("\\n"), encoding="utf-8")'
    ),
    "'''def commit_mark(": "'''    def commit_mark(",
    "'''def load_commands(": "'''    def load_commands(",
}
for old, new in replacements.items():
    count = text.count(old)
    if count == 1:
        text = text.replace(old, new)
    elif count == 0 and new in text:
        continue
    else:
        raise SystemExit(
            f"expected one lifecycle patch source or existing target for {old!r}, "
            f"found {count}"
        )
path.write_text(text, encoding="utf-8")

round4 = Path("scripts/patch_portfolio_lifecycle_round4.py")
round4_text = round4.read_text(encoding="utf-8")
old = '''tail = tail.replace(
    \'''            mark_price=delta.price,
            position_margin=position.position_margin - margin_release,
            initial_risk=position.initial_risk - risk_release,
            updated_at=update.event_time,
\''',
    \'''            mark_price=(
                position.mark_price
                if position.last_mark_time is not None
                and position.last_mark_time >= update.event_time
                else delta.price
            ),
            position_margin=position.position_margin - margin_release,
            initial_risk=position.initial_risk - risk_release,
            last_execution_time=update.event_time,
            updated_at=max(position.updated_at, update.event_time),
\''',
)
'''
new = '''tail = tail.replace(
    "            mark_price=delta.price,\\n",
    \'''            mark_price=(
                position.mark_price
                if position.last_mark_time is not None
                and position.last_mark_time >= update.event_time
                else delta.price
            ),
\''',
)
tail = tail.replace(
    "            updated_at=update.event_time,\\n",
    \'''            last_execution_time=update.event_time,
            updated_at=max(position.updated_at, update.event_time),
\''',
)
'''
count = round4_text.count(old)
if count == 1:
    round4.write_text(round4_text.replace(old, new), encoding="utf-8")
elif count == 0 and new in round4_text:
    pass
else:
    raise SystemExit(
        f"expected one close-fill time-axis source or existing target, found {count}"
    )

finalization = Path("scripts/patch_portfolio_runtime_audit_propagation.py")
finalization_text = finalization.read_text(encoding="utf-8")
old = '''            and position.last_mark_time > event_time
            else selected_mark
'''
new = '''            and position.last_mark_time >= event_time
            else selected_mark
'''
count = finalization_text.count(old)
if count == 1:
    finalization.write_text(finalization_text.replace(old, new), encoding="utf-8")
elif count == 0 and new in finalization_text:
    pass
else:
    raise SystemExit(
        f"expected one funding/mark priority source or existing target, found {count}"
    )

finalization_text = finalization.read_text(encoding="utf-8")
late_execution_marker = "# Portfolio late-execution-after-funding hardening"
if late_execution_marker not in finalization_text:
    finalization.write_text(
        finalization_text.rstrip()
        + "\n\n"
        + r"""# Portfolio late-execution-after-funding hardening

def _insert_execution_after_funding_barrier(
    path: Path,
    *,
    function_name: str,
    anchor: str,
) -> None:
    text = path.read_text(encoding="utf-8")
    marker = "execution fill cannot be applied at or before settled funding"
    start = text.index(f"def {function_name}(")
    end = text.find("\ndef ", start + 1)
    if end < 0:
        end = len(text)
    if marker in text[start:end]:
        return
    anchor_index = text.index(anchor, start, end)
    insertion = '''    if (
        position is not None
        and position.last_funding_time is not None
        and update.event_time <= position.last_funding_time
    ):
        raise PortfolioAccountingError(
            "execution fill cannot be applied at or before settled funding"
        )
'''
    path.write_text(
        text[:anchor_index] + insertion + text[anchor_index:],
        encoding="utf-8",
    )


_accounting = Path("src/crypto_quant/portfolio_accounting.py")
_insert_execution_after_funding_barrier(
    _accounting,
    function_name="apply_open_fill",
    anchor="    expected_side = (\n",
)
_insert_execution_after_funding_barrier(
    _accounting,
    function_name="apply_close_fill",
    anchor="    expected_action = (\n",
)

_test = Path("tests/test_portfolio_lifecycle.py")
_test_text = _test.read_text(encoding="utf-8")
_test_marker = "def test_execution_at_or_before_settled_funding_fails_closed("
if _test_marker not in _test_text:
    _test.write_text(
        _test_text.rstrip()
        + "\n\n"
        + '''def test_execution_at_or_before_settled_funding_fails_closed() -> None:
    policy, balances, _, command, opened = opened_position()
    position = opened.position
    assert position is not None
    funded = apply_funding_to_position(
        account_id="paper",
        position=position,
        rate=Decimal("0.0001"),
        mark_price=Decimal("101"),
        event_time=NOW + 3,
        source_id="execution-after-funding-boundary",
        policy=policy,
    )
    assert funded.transaction is not None
    balances = balances.apply(funded.transaction)
    protective = next(
        order for order in command.orders
        if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    quantity = funded.position.quantity / Decimal("2")

    with pytest.raises(
        RuntimeError,
        match="at or before settled funding",
    ):
        apply_close_fill(
            command=command,
            order=protective,
            update=cumulative_update(
                protective,
                update_id="late-close-after-funding",
                cumulative_quantity=quantity,
                cumulative_quote_value=quantity * Decimal("99"),
                cumulative_fee=Decimal("0.01"),
                event_time=NOW + 3,
                state=OrderState.PARTIALLY_FILLED,
            ),
            cursor=OrderAccountingCursor(
                protective.order_id,
                protective.command_id,
            ),
            reservation=opened.reservation,
            balances=balances,
            policy=policy,
            position=funded.position,
        )
''',
        encoding="utf-8",
    )
""",
        encoding="utf-8",
    )
