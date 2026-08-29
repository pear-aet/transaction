from decimal import Decimal

from crypto_quant.execution_contracts import OrderLegType, OrderState
from crypto_quant.portfolio_contracts import AccountPolicy
from crypto_quant.research_accounting import ResearchAccount

from portfolio_fixtures import NOW, cumulative_update, execution_command, risk_decision


def test_research_account_partial_open_and_close_use_cumulative_deltas() -> None:
    policy = AccountPolicy()
    decision = risk_decision()
    command = execution_command(decision)
    quantity = Decimal(str(command.quantity))
    first_quantity = quantity / Decimal("2")
    second_quantity = quantity - first_quantity
    primary = next(order for order in command.orders if order.leg_type is OrderLegType.PRIMARY)
    protective = next(
        order for order in command.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    account = ResearchAccount.initialized(
        account_id="paper",
        initial_capital="10000",
        event_time=NOW - 1,
        source_id="research-partial-capital",
        policy=policy,
    )
    account.reserve(command=command, decision=decision, now_ms=NOW)

    first_open_quote = first_quantity * Decimal("100")
    first_open_fee = policy.quantize_money(first_open_quote * Decimal("0.001"))
    first_position = account.apply_open_update(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="research-partial-open-1",
            cumulative_quantity=first_quantity,
            cumulative_quote_value=first_open_quote,
            cumulative_fee=first_open_fee,
            event_time=NOW + 1,
            state=OrderState.PARTIALLY_FILLED,
        ),
        maintenance_margin_rate=Decimal("0.005"),
    )
    assert first_position.quantity == first_quantity
    assert first_position.average_entry_price == Decimal("100")

    total_open_quote = first_open_quote + second_quantity * Decimal("102")
    total_open_fee = policy.quantize_money(total_open_quote * Decimal("0.001"))
    full_position = account.apply_open_update(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="research-partial-open-2",
            cumulative_quantity=quantity,
            cumulative_quote_value=total_open_quote,
            cumulative_fee=total_open_fee,
            event_time=NOW + 2,
            state=OrderState.FILLED,
        ),
        maintenance_margin_rate=Decimal("0.005"),
    )
    expected_entry = total_open_quote / quantity
    assert full_position.quantity == quantity
    assert full_position.total_opened_quantity == quantity
    assert full_position.average_entry_price == expected_entry
    assert full_position.opening_fees == total_open_fee

    first_close_quote = first_quantity * Decimal("99")
    first_close_fee = policy.quantize_money(first_close_quote * Decimal("0.001"))
    remaining = account.apply_close_update(
        command=command,
        order=protective,
        update=cumulative_update(
            protective,
            update_id="research-partial-close-1",
            cumulative_quantity=first_quantity,
            cumulative_quote_value=first_close_quote,
            cumulative_fee=first_close_fee,
            event_time=NOW + 3,
            state=OrderState.PARTIALLY_FILLED,
        ),
    )
    assert remaining is not None
    assert remaining.quantity == second_quantity
    assert remaining.average_entry_price == expected_entry
    assert remaining.cumulative_closed_quantity == first_quantity
    assert remaining.average_exit_price == Decimal("99")
    assert remaining.position_margin < full_position.position_margin
    assert remaining.initial_risk < full_position.initial_risk

    total_close_quote = first_close_quote + second_quantity * Decimal("98")
    total_close_fee = policy.quantize_money(total_close_quote * Decimal("0.001"))
    closed_state = account.apply_close_update(
        command=command,
        order=protective,
        update=cumulative_update(
            protective,
            update_id="research-partial-close-2",
            cumulative_quantity=quantity,
            cumulative_quote_value=total_close_quote,
            cumulative_fee=total_close_fee,
            event_time=NOW + 4,
            state=OrderState.FILLED,
        ),
    )
    assert closed_state is None
    assert len(account.closed_positions) == 1
    closed = account.closed_positions[0]
    expected_realized = (
        first_quantity * (Decimal("99") - expected_entry)
        + second_quantity * (Decimal("98") - expected_entry)
    )
    assert closed.total_quantity == quantity
    assert closed.average_entry_price == expected_entry
    assert closed.average_exit_price == total_close_quote / quantity
    assert closed.realized_pnl == policy.quantize_money(expected_realized)
    assert closed.opening_fees == total_open_fee
    assert closed.closing_fees == total_close_fee

    account.release(
        command_id=command.command_id,
        event_time=NOW + 5,
        reason="research_partial_complete",
    )
    expected_final_cash = (
        Decimal("10000")
        + closed.realized_pnl
        - closed.opening_fees
        - closed.closing_fees
    )
    assert account.balances.cash == expected_final_cash
    assert account.balances.command_margin == Decimal("0")
    assert account.balances.position_margin == Decimal("0")
    assert account.balances.command_fee_reserve == Decimal("0")
