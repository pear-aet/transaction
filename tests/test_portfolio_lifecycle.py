from decimal import Decimal

import pytest

from crypto_quant.execution_contracts import OrderLegType, OrderState
from crypto_quant.portfolio_accounting import (
    OrderAccountingCursor,
    apply_close_fill,
    apply_funding_to_position,
    apply_mark_fact,
    apply_open_fill,
    initialize_account_transaction,
    plan_command_reservation,
)
from crypto_quant.portfolio_contracts import AccountBalances, AccountPolicy
from crypto_quant.portfolio_facts import (
    ClosedPosition,
    ReconciliationKind,
    ReconciliationSeverity,
    make_mark_price_fact,
    make_reconciliation_finding,
)

from portfolio_fixtures import NOW, cumulative_update, execution_command, risk_decision


def initialized_balances(policy: AccountPolicy) -> AccountBalances:
    return AccountBalances().apply(
        initialize_account_transaction(
            account_id="paper",
            initial_capital="10000",
            event_time=NOW - 1,
            source_id="initial-lifecycle-capital",
            policy=policy,
        )
    )


def opened_position():
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
    first_quote = first_quantity * Decimal("100")
    first_fee = policy.quantize_money(first_quote * Decimal("0.001"))
    first = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="lifecycle-open-1",
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
    balances = balances.apply(first.transaction)
    second_quote = first_quantity * Decimal("102")
    second_fee = policy.quantize_money(second_quote * Decimal("0.001"))
    second = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="lifecycle-open-2",
            cumulative_quantity=quantity,
            cumulative_quote_value=first_quote + second_quote,
            cumulative_fee=first_fee + second_fee,
            event_time=NOW + 2,
            state=OrderState.FILLED,
        ),
        cursor=first.cursor,
        reservation=first.reservation,
        balances=balances,
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
        position=first.position,
    )
    balances = balances.apply(second.transaction)
    assert second.position is not None
    return policy, balances, decision, command, second


def test_partial_close_funding_and_full_close_produce_one_complete_trade_fact() -> None:
    policy, balances, _, command, opened = opened_position()
    position = opened.position
    assert position is not None
    assert position.opening_fees > 0
    assert position.total_opened_quantity == position.quantity

    funded = apply_funding_to_position(
        account_id="paper",
        position=position,
        rate=Decimal("0.0001"),
        mark_price=Decimal("101"),
        event_time=NOW + 3,
        source_id="lifecycle-funding-1",
        policy=policy,
    )
    balances = balances.apply(funded.transaction)
    assert funded.signed_funding_pnl < 0
    assert funded.position.funding_pnl == funded.signed_funding_pnl

    protective = next(
        order for order in command.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    total_quantity = funded.position.quantity
    first_close_quantity = total_quantity / 3
    first_close_quote = first_close_quantity * Decimal("99")
    first_close_fee = policy.quantize_money(first_close_quote * Decimal("0.001"))
    first_close = apply_close_fill(
        command=command,
        order=protective,
        update=cumulative_update(
            protective,
            update_id="lifecycle-close-1",
            cumulative_quantity=first_close_quantity,
            cumulative_quote_value=first_close_quote,
            cumulative_fee=first_close_fee,
            event_time=NOW + 4,
            state=OrderState.PARTIALLY_FILLED,
        ),
        cursor=OrderAccountingCursor(protective.order_id, protective.command_id),
        reservation=opened.reservation,
        balances=balances,
        policy=policy,
        position=funded.position,
    )
    balances = balances.apply(first_close.transaction)
    assert first_close.position is not None
    assert first_close.closed_position is None
    assert first_close.position.cumulative_closed_quantity == first_close_quantity
    assert first_close.position.cumulative_close_quote_value == first_close_quote
    assert first_close.position.realized_pnl == first_close.realized_pnl
    assert first_close.position.closing_fees == first_close_fee
    assert first_close.position.total_opened_quantity == total_quantity

    remaining = first_close.position.quantity
    second_close_quote = remaining * Decimal("103")
    second_close_fee = policy.quantize_money(second_close_quote * Decimal("0.001"))
    final = apply_close_fill(
        command=command,
        order=protective,
        update=cumulative_update(
            protective,
            update_id="lifecycle-close-2",
            cumulative_quantity=total_quantity,
            cumulative_quote_value=first_close_quote + second_close_quote,
            cumulative_fee=first_close_fee + second_close_fee,
            event_time=NOW + 5,
            state=OrderState.FILLED,
        ),
        cursor=first_close.cursor,
        reservation=first_close.reservation,
        balances=balances,
        policy=policy,
        position=first_close.position,
    )
    balances = balances.apply(final.transaction)

    assert final.position is None
    assert final.position_closed is True
    assert final.closed_position is not None
    closed = final.closed_position
    assert closed.total_quantity == total_quantity
    assert closed.average_entry_price == position.average_entry_price
    assert closed.average_exit_price == (
        first_close_quote + second_close_quote
    ) / total_quantity
    assert closed.realized_pnl == first_close.realized_pnl + final.realized_pnl
    assert closed.opening_fees == position.opening_fees
    assert closed.closing_fees == first_close_fee + second_close_fee
    assert closed.funding_pnl == funded.position.funding_pnl
    assert closed.net_pnl == (
        closed.realized_pnl
        + closed.funding_pnl
        - closed.opening_fees
        - closed.closing_fees
    )
    assert ClosedPosition.from_dict(closed.to_dict()) == closed
    assert balances.settled_assets == balances.accounting_equity


def test_funding_time_and_mark_fact_revision_fail_closed() -> None:
    policy, _, _, _, opened = opened_position()
    position = opened.position
    assert position is not None
    funded = apply_funding_to_position(
        account_id="paper",
        position=position,
        rate=Decimal("0.0001"),
        mark_price=Decimal("101"),
        event_time=NOW + 3,
        source_id="funding-time-1",
        policy=policy,
    )
    with pytest.raises(Exception, match="did not advance"):
        apply_funding_to_position(
            account_id="paper",
            position=funded.position,
            rate=Decimal("0.0002"),
            mark_price=Decimal("101"),
            event_time=NOW + 3,
            source_id="funding-time-2",
            policy=policy,
        )

    fact = make_mark_price_fact(
        account_id="paper",
        source_event_id="mark-1",
        symbol=position.symbol,
        observed_at=NOW + 4,
        source_sequence=10,
        mark_price=102,
    )
    marked = apply_mark_fact(funded.position, fact)
    assert marked.position is not None
    assert marked.position.mark_price == Decimal("102")
    assert marked.position.unrealized_pnl != funded.position.unrealized_pnl

    with pytest.raises(ValueError, match="ID does not match"):
        make_mark_price_fact(
            account_id="paper",
            source_event_id="mark-2",
            symbol=position.symbol,
            observed_at=NOW + 4,
            source_sequence=11,
            mark_price=103,
        ).__class__(
            fact_id="forged",
            account_id="paper",
            source_event_id="mark-2",
            symbol=position.symbol,
            observed_at=NOW + 4,
            source_sequence=11,
            mark_price=Decimal("103"),
        )


def test_reconciliation_finding_identity_and_serialization_are_deterministic() -> None:
    first = make_reconciliation_finding(
        account_id="paper",
        kind=ReconciliationKind.EXECUTION_FILL,
        severity=ReconciliationSeverity.CRITICAL,
        source_id="command-1",
        found_at=NOW,
        expected={"filled": Decimal("2")},
        actual={"filled": Decimal("1")},
        evidence={"order_id": "order-1"},
    )
    second = make_reconciliation_finding(
        account_id="paper",
        kind=ReconciliationKind.EXECUTION_FILL,
        severity=ReconciliationSeverity.CRITICAL,
        source_id="command-1",
        found_at=NOW,
        expected={"filled": Decimal("2")},
        actual={"filled": Decimal("1")},
        evidence={"order_id": "order-1"},
    )
    assert first == second
    assert first.from_dict(first.to_dict()) == first
