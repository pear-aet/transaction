from decimal import Decimal

import pytest

from crypto_quant.execution_contracts import OrderLegType, OrderState
from crypto_quant.portfolio_accounting import (
    OrderAccountingCursor,
    PortfolioAccountingError,
    apply_close_fill,
    apply_mark_price,
    apply_open_fill,
    derive_fill_delta,
    funding_transaction,
    initialize_account_transaction,
    plan_command_reservation,
    release_command_reservation,
)
from crypto_quant.portfolio_contracts import (
    AccountBalances,
    AccountPolicy,
    PortfolioPosition,
    PositionSide,
    ReservationState,
)
from crypto_quant.strategy_contracts import StrategyAction

from .portfolio_fixtures import (
    NOW,
    cumulative_update,
    execution_command,
    risk_decision,
)


def initialized_balances(policy: AccountPolicy) -> AccountBalances:
    return AccountBalances().apply(
        initialize_account_transaction(
            account_id="paper",
            initial_capital="10000",
            event_time=NOW - 1,
            source_id="initial-capital",
            policy=policy,
        )
    )


def test_partial_open_fills_use_cumulative_deltas_and_weighted_average() -> None:
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
        now_ms=NOW,
    )
    assert plan.transaction is not None
    balances = balances.apply(plan.transaction)
    primary = next(order for order in command.orders if order.leg_type is OrderLegType.PRIMARY)
    quantity = Decimal(str(command.quantity))
    first_quantity = quantity / 2
    second_quantity = quantity - first_quantity
    first_quote = first_quantity * Decimal("100")
    second_quote = second_quantity * Decimal("102")
    first_fee = policy.quantize_money(first_quote * Decimal("0.001"))
    second_fee = policy.quantize_money(second_quote * Decimal("0.001"))
    cursor = OrderAccountingCursor(primary.order_id, primary.command_id)

    first_update = cumulative_update(
        primary,
        update_id="open-fill-1",
        cumulative_quantity=first_quantity,
        cumulative_quote_value=first_quote,
        cumulative_fee=first_fee,
        event_time=NOW + 1,
        state=OrderState.PARTIALLY_FILLED,
    )
    first = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=first_update,
        cursor=cursor,
        reservation=plan.reservation,
        balances=balances,
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
    )
    balances = balances.apply(first.transaction)
    assert first.position is not None
    assert first.position.quantity == first_quantity
    assert first.position.average_entry_price == Decimal("100")
    assert first.position.stop_price == Decimal(str(decision.stop_price))
    assert first.cursor.cumulative_quote_value == policy.quantize_money(first_quote)

    total_quote = first_quote + second_quote
    total_fee = first_fee + second_fee
    second_update = cumulative_update(
        primary,
        update_id="open-fill-2",
        cumulative_quantity=quantity,
        cumulative_quote_value=total_quote,
        cumulative_fee=total_fee,
        event_time=NOW + 2,
        state=OrderState.FILLED,
    )
    second = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=second_update,
        cursor=first.cursor,
        reservation=first.reservation,
        balances=balances,
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
        position=first.position,
    )
    balances = balances.apply(second.transaction)

    assert second.position is not None
    assert second.position.quantity == quantity
    assert second.position.average_entry_price == total_quote / quantity
    assert second.reservation.command_margin_remaining == 0
    assert balances.settled_assets == balances.accounting_equity
    assert balances.fees == total_fee


def test_cumulative_update_replay_and_backwards_values_fail_closed() -> None:
    policy = AccountPolicy()
    command = execution_command()
    primary = command.orders[0]
    quantity = Decimal(str(command.quantity)) / 2
    quote = quantity * Decimal("100")
    fee = policy.quantize_money(quote * Decimal("0.001"))
    cursor = OrderAccountingCursor(primary.order_id, primary.command_id)
    first_update = cumulative_update(
        primary,
        update_id="cursor-1",
        cumulative_quantity=quantity,
        cumulative_quote_value=quote,
        cumulative_fee=fee,
        event_time=NOW + 1,
        state=OrderState.PARTIALLY_FILLED,
    )
    _, cursor = derive_fill_delta(
        cursor=cursor,
        order=primary,
        update=first_update,
        policy=policy,
    )
    duplicate_values = cumulative_update(
        primary,
        update_id="cursor-2",
        cumulative_quantity=quantity,
        cumulative_quote_value=quote,
        cumulative_fee=fee,
        event_time=NOW + 2,
        state=OrderState.PARTIALLY_FILLED,
    )
    with pytest.raises(PortfolioAccountingError, match="no new accounting fact"):
        derive_fill_delta(
            cursor=cursor,
            order=primary,
            update=duplicate_values,
            policy=policy,
        )

    backwards = cumulative_update(
        primary,
        update_id="cursor-3",
        cumulative_quantity=quantity / 2,
        cumulative_quote_value=quote / 2,
        cumulative_fee=fee / 2,
        event_time=NOW + 3,
        state=OrderState.PARTIALLY_FILLED,
    )
    with pytest.raises(PortfolioAccountingError, match="moved backwards"):
        derive_fill_delta(
            cursor=cursor,
            order=primary,
            update=backwards,
            policy=policy,
        )


def test_protective_close_realizes_pnl_and_releases_position_margin() -> None:
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
        now_ms=NOW,
    )
    assert plan.transaction is not None
    balances = balances.apply(plan.transaction)
    primary, protective = command.orders
    quantity = Decimal(str(command.quantity))
    open_price = Decimal("100")
    open_quote = quantity * open_price
    open_fee = policy.quantize_money(open_quote * Decimal("0.001"))
    opened = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="open-full",
            cumulative_quantity=quantity,
            cumulative_quote_value=open_quote,
            cumulative_fee=open_fee,
            event_time=NOW + 1,
            state=OrderState.FILLED,
        ),
        cursor=OrderAccountingCursor(primary.order_id, primary.command_id),
        reservation=plan.reservation,
        balances=balances,
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
    )
    balances = balances.apply(opened.transaction)
    assert opened.position is not None

    close_price = Decimal("99")
    close_quote = quantity * close_price
    close_fee = policy.quantize_money(close_quote * Decimal("0.001"))
    closed = apply_close_fill(
        command=command,
        order=protective,
        update=cumulative_update(
            protective,
            update_id="protective-close",
            cumulative_quantity=quantity,
            cumulative_quote_value=close_quote,
            cumulative_fee=close_fee,
            event_time=NOW + 2,
            state=OrderState.FILLED,
        ),
        cursor=OrderAccountingCursor(protective.order_id, protective.command_id),
        reservation=opened.reservation,
        balances=balances,
        policy=policy,
        position=opened.position,
    )
    balances = balances.apply(closed.transaction)

    assert closed.position is None
    assert closed.position_closed is True
    assert closed.realized_pnl == quantity * (close_price - open_price)
    assert balances.position_margin == 0
    assert balances.realized_pnl == closed.realized_pnl
    assert balances.settled_assets == balances.accounting_equity

    released = release_command_reservation(
        reservation=closed.reservation,
        policy=policy,
        event_time=NOW + 3,
        reason="command_completed",
    )
    if released.transaction is not None:
        balances = balances.apply(released.transaction)
    assert released.reservation.state is ReservationState.RELEASED
    assert released.reservation.command_margin_remaining == 0
    assert released.reservation.fee_reserve_remaining == 0
    assert balances.command_fee_reserve == 0
    assert balances.command_margin == 0


def test_reduce_only_over_close_and_wrong_command_fail_closed() -> None:
    policy = AccountPolicy()
    balances = AccountBalances(
        cash=Decimal("9900"),
        position_margin=Decimal("100"),
        external_capital=Decimal("10000"),
    )
    position = PortfolioPosition(
        position_id="position-1",
        account_id="paper",
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        average_entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        stop_price=Decimal("99"),
        position_margin=Decimal("100"),
        initial_risk=Decimal("1"),
        maintenance_margin_rate=Decimal("0.005"),
        correlation_group="default",
        open_command_id="different-command",
        open_risk_decision_id="risk-1",
        open_intent_id="intent-1",
        opened_at=NOW,
        updated_at=NOW,
    )
    decision = risk_decision()
    command = execution_command(decision)
    plan = plan_command_reservation(
        account_id="paper",
        command=command,
        decision=decision,
        balances=balances,
        policy=policy,
        now_ms=NOW,
    )
    protective = command.orders[1]
    quantity = Decimal("1.1")

    with pytest.raises(PortfolioAccountingError, match="exceeds the open position"):
        apply_close_fill(
            command=command,
            order=protective,
            update=cumulative_update(
                protective,
                update_id="over-close",
                cumulative_quantity=quantity,
                cumulative_quote_value=quantity * Decimal("99"),
                cumulative_fee=Decimal("0.1"),
                event_time=NOW + 1,
                state=OrderState.PARTIALLY_FILLED,
            ),
            cursor=OrderAccountingCursor(protective.order_id, protective.command_id),
            reservation=plan.reservation,
            balances=balances,
            policy=policy,
            position=position,
        )

    quantity = Decimal("0.5")
    with pytest.raises(PortfolioAccountingError, match="another open command"):
        apply_close_fill(
            command=command,
            order=protective,
            update=cumulative_update(
                protective,
                update_id="wrong-protection",
                cumulative_quantity=quantity,
                cumulative_quote_value=quantity * Decimal("99"),
                cumulative_fee=Decimal("0.05"),
                event_time=NOW + 1,
                state=OrderState.PARTIALLY_FILLED,
            ),
            cursor=OrderAccountingCursor(protective.order_id, protective.command_id),
            reservation=plan.reservation,
            balances=balances,
            policy=policy,
            position=position,
        )


def test_exit_reservations_cannot_overlock_position() -> None:
    policy = AccountPolicy()
    balances = initialized_balances(policy)
    decision = risk_decision(StrategyAction.EXIT, storage_sequence=2)
    command = execution_command(decision)
    position = PortfolioPosition(
        position_id="position-exit",
        account_id="paper",
        symbol=command.symbol,
        side=PositionSide.LONG,
        quantity=Decimal("2"),
        average_entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        stop_price=Decimal("90"),
        position_margin=Decimal("100"),
        initial_risk=Decimal("20"),
        maintenance_margin_rate=Decimal("0.005"),
        correlation_group="default",
        open_command_id="open-command",
        open_risk_decision_id="open-risk",
        open_intent_id="open-intent",
        opened_at=NOW - 10,
        updated_at=NOW,
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
    assert plan.reservation.reserved_exit_quantity == Decimal(str(command.quantity))

    with pytest.raises(PortfolioAccountingError, match="exceed the open position"):
        plan_command_reservation(
            account_id="paper",
            command=command,
            decision=decision,
            balances=balances,
            policy=policy,
            now_ms=NOW,
            current_position=position,
            already_locked_exit_quantity=Decimal("0.001"),
        )


def test_funding_sign_and_mark_revision_are_deterministic() -> None:
    policy = AccountPolicy()
    position = PortfolioPosition(
        position_id="funding-position",
        account_id="paper",
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        quantity=Decimal("2"),
        average_entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        stop_price=Decimal("99"),
        position_margin=Decimal("100"),
        initial_risk=Decimal("2"),
        maintenance_margin_rate=Decimal("0.005"),
        correlation_group="default",
        open_command_id="command-1",
        open_risk_decision_id="risk-1",
        open_intent_id="intent-1",
        opened_at=NOW,
        updated_at=NOW,
    )
    funding = funding_transaction(
        account_id="paper",
        position=position,
        rate=Decimal("0.0001"),
        mark_price=Decimal("100"),
        event_time=NOW + 1,
        source_id="funding-1",
        policy=policy,
    )
    balances = AccountBalances(
        cash=Decimal("9900"),
        position_margin=Decimal("100"),
        external_capital=Decimal("10000"),
    ).apply(funding)
    assert balances.cash == Decimal("9899.98000000")
    assert balances.funding_pnl == Decimal("-0.02000000")
    assert balances.settled_assets == balances.accounting_equity

    marked = apply_mark_price(position, mark_price=101, observed_at=NOW + 2)
    assert marked.unrealized_pnl == Decimal("2")
    assert apply_mark_price(marked, mark_price=101, observed_at=NOW + 2) == marked
    with pytest.raises(PortfolioAccountingError, match="revision"):
        apply_mark_price(marked, mark_price=102, observed_at=NOW + 2)
    with pytest.raises(PortfolioAccountingError, match="moved backwards"):
        apply_mark_price(marked, mark_price=100, observed_at=NOW + 1)
