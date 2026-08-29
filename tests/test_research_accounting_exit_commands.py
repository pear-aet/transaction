from decimal import Decimal

import pytest

from crypto_quant.execution_contracts import OrderLegType, OrderState
from crypto_quant.portfolio_accounting import PortfolioAccountingError
from crypto_quant.portfolio_contracts import AccountPolicy
from crypto_quant.research_accounting import ResearchAccount
from crypto_quant.strategy_contracts import StrategyAction

from portfolio_fixtures import NOW, cumulative_update, execution_command, risk_decision


def test_explicit_exit_command_locks_quantity_and_closes_original_position() -> None:
    policy = AccountPolicy()
    entry_decision = risk_decision(storage_sequence=1)
    entry_command = execution_command(entry_decision)
    quantity = Decimal(str(entry_command.quantity))
    entry_price = Decimal("100")
    entry_quote = quantity * entry_price
    entry_fee = policy.quantize_money(entry_quote * Decimal("0.001"))
    account = ResearchAccount.initialized(
        account_id="paper",
        initial_capital="10000",
        event_time=NOW - 1,
        source_id="research-exit-capital",
        policy=policy,
    )
    account.reserve(command=entry_command, decision=entry_decision, now_ms=NOW)
    entry_order = next(
        order for order in entry_command.orders if order.leg_type is OrderLegType.PRIMARY
    )
    opened = account.apply_open_update(
        command=entry_command,
        decision=entry_decision,
        order=entry_order,
        update=cumulative_update(
            entry_order,
            update_id="research-exit-open",
            cumulative_quantity=quantity,
            cumulative_quote_value=entry_quote,
            cumulative_fee=entry_fee,
            event_time=NOW + 1,
            state=OrderState.FILLED,
        ),
        maintenance_margin_rate=Decimal("0.005"),
    )

    exit_decision = risk_decision(
        StrategyAction.EXIT,
        storage_sequence=2,
        position_side="long",
        position_quantity=float(opened.quantity),
        position_entry_price=float(opened.average_entry_price),
        position_mark_price=float(opened.mark_price),
        position_initial_risk=float(opened.initial_risk),
        position_stop_price=float(opened.stop_price),
    )
    exit_command = execution_command(exit_decision)
    exit_reservation = account.reserve(
        command=exit_command,
        decision=exit_decision,
        now_ms=NOW + 2,
    )
    assert exit_reservation.entry is False
    assert exit_reservation.reserved_exit_quantity == opened.quantity
    assert exit_reservation.reserved_margin_total == Decimal("0")

    competing_decision = risk_decision(
        StrategyAction.EXIT,
        storage_sequence=3,
        position_side="long",
        position_quantity=float(opened.quantity),
        position_entry_price=float(opened.average_entry_price),
        position_mark_price=float(opened.mark_price),
        position_initial_risk=float(opened.initial_risk),
        position_stop_price=float(opened.stop_price),
    )
    competing_command = execution_command(competing_decision)
    with pytest.raises(PortfolioAccountingError, match="exit|lock|quantity"):
        account.reserve(
            command=competing_command,
            decision=competing_decision,
            now_ms=NOW + 2,
        )

    exit_order = next(
        order for order in exit_command.orders if order.leg_type is OrderLegType.PRIMARY
    )
    assert exit_order.reduce_only is True
    exit_price = Decimal("103")
    exit_quote = quantity * exit_price
    exit_fee = policy.quantize_money(exit_quote * Decimal("0.001"))
    assert (
        account.apply_close_update(
            command=exit_command,
            order=exit_order,
            update=cumulative_update(
                exit_order,
                update_id="research-explicit-exit",
                cumulative_quantity=quantity,
                cumulative_quote_value=exit_quote,
                cumulative_fee=exit_fee,
                event_time=NOW + 3,
                state=OrderState.FILLED,
            ),
        )
        is None
    )
    account.release(
        command_id=exit_command.command_id,
        event_time=NOW + 4,
        reason="explicit_exit_complete",
    )
    account.release(
        command_id=entry_command.command_id,
        event_time=NOW + 4,
        reason="entry_protection_no_longer_needed",
    )

    assert len(account.closed_positions) == 1
    closed = account.closed_positions[0]
    expected_realized = policy.quantize_money(quantity * (exit_price - entry_price))
    expected_net = expected_realized - entry_fee - exit_fee
    assert closed.open_command_id == entry_command.command_id
    assert closed.close_command_id == exit_command.command_id
    assert closed.realized_pnl == expected_realized
    assert closed.net_pnl == expected_net
    assert account.balances.cash == Decimal("10000") + expected_net
    assert account.balances.command_margin == Decimal("0")
    assert account.balances.position_margin == Decimal("0")
    assert account.balances.command_fee_reserve == Decimal("0")
