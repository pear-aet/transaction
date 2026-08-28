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
from crypto_quant.portfolio_contracts import AccountPolicy
from crypto_quant.portfolio_facts import (
    ReconciliationKind,
    ReconciliationSeverity,
    make_mark_price_fact,
    make_reconciliation_finding,
)
from crypto_quant.portfolio_store import AccountStatus, AccountStoreConflict, PortfolioStore

from portfolio_fixtures import NOW, cumulative_update, execution_command, risk_decision


def setup_open_position(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.db")
    policy = AccountPolicy()
    initialized = store.initialize_account(
        account_id="paper",
        policy=policy,
        transaction=initialize_account_transaction(
            account_id="paper",
            initial_capital="10000",
            event_time=NOW - 1,
            source_id="lifecycle-store-capital",
            policy=policy,
        ),
    )
    decision = risk_decision()
    command = execution_command(decision)
    reserved = store.commit_reservation(
        plan_command_reservation(
            account_id="paper",
            command=command,
            decision=decision,
            balances=initialized.balances,
            policy=policy,
            now_ms=NOW,
        ),
        policy,
    )
    primary = next(order for order in command.orders if order.leg_type is OrderLegType.PRIMARY)
    quantity = Decimal(str(command.quantity))
    quote = quantity * Decimal("100")
    fee = policy.quantize_money(quote * Decimal("0.001"))
    opened = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="store-open-fill",
            cumulative_quantity=quantity,
            cumulative_quote_value=quote,
            cumulative_fee=fee,
            event_time=NOW + 1,
            state=OrderState.FILLED,
        ),
        cursor=store.load_order_cursor(primary.order_id, primary.command_id),
        reservation=reserved.reservation,
        balances=reserved.balances,
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
    )
    committed = store.commit_fill(opened, policy)
    assert committed.position is not None
    return store, policy, decision, command, committed


def test_mark_and_funding_update_position_atomically_and_are_idempotent(tmp_path) -> None:
    store, policy, _, _, opened = setup_open_position(tmp_path)
    position = opened.position
    assert position is not None
    fact = make_mark_price_fact(
        account_id="paper",
        source_event_id="store-mark-1",
        symbol=position.symbol,
        observed_at=NOW + 2,
        source_sequence=10,
        mark_price=101,
    )
    result = apply_mark_fact(position, fact)
    first_mark = store.commit_mark(result, policy)
    duplicate_mark = store.commit_mark(result, policy)
    assert first_mark.duplicate is False
    assert duplicate_mark.duplicate is True
    assert first_mark.fact.storage_sequence == 1
    assert store.latest_mark("paper", position.symbol) == first_mark.fact
    assert store.load_position("paper", position.symbol) == first_mark.position

    current = first_mark.position
    assert current is not None
    funding = apply_funding_to_position(
        account_id="paper",
        position=current,
        rate=Decimal("0.0001"),
        mark_price=Decimal("101"),
        event_time=NOW + 3,
        source_id="store-funding-1",
        policy=policy,
    )
    first_funding = store.commit_funding(
        account_id="paper",
        symbol=current.symbol,
        source_id="store-funding-1",
        result=funding,
        policy=policy,
    )
    duplicate_funding = store.commit_funding(
        account_id="paper",
        symbol=current.symbol,
        source_id="store-funding-1",
        result=funding,
        policy=policy,
    )
    assert first_funding.duplicate is False
    assert duplicate_funding.duplicate is True
    assert first_funding.position.funding_pnl == funding.signed_funding_pnl
    assert store.load_position("paper", current.symbol) == first_funding.position
    assert store.balances("paper") == first_funding.balances

    revision = make_mark_price_fact(
        account_id="paper",
        source_event_id="store-mark-revision",
        symbol=current.symbol,
        observed_at=NOW + 2,
        source_sequence=11,
        mark_price=102,
    )
    with pytest.raises(AccountStoreConflict, match="moved backwards|revision"):
        store.commit_mark(apply_mark_fact(first_funding.position, revision), policy)


def test_full_close_and_closed_position_commit_in_one_transaction(tmp_path) -> None:
    store, policy, _, command, opened = setup_open_position(tmp_path)
    position = opened.position
    assert position is not None
    protective = next(
        order for order in command.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    quantity = position.quantity
    quote = quantity * Decimal("99")
    fee = policy.quantize_money(quote * Decimal("0.001"))
    closed = apply_close_fill(
        command=command,
        order=protective,
        update=cumulative_update(
            protective,
            update_id="store-close-fill",
            cumulative_quantity=quantity,
            cumulative_quote_value=quote,
            cumulative_fee=fee,
            event_time=NOW + 2,
            state=OrderState.FILLED,
        ),
        cursor=store.load_order_cursor(protective.order_id, protective.command_id),
        reservation=opened.reservation,
        balances=opened.balances,
        policy=policy,
        position=position,
    )
    first = store.commit_fill(closed, policy)
    second = store.commit_fill(closed, policy)

    assert first.duplicate is False
    assert second.duplicate is True
    assert first.position is None
    assert first.closed_position is not None
    assert first.closed_position.storage_sequence == 1
    assert second.closed_position == first.closed_position
    assert store.load_position("paper", command.symbol) is None
    assert store.load_closed_position(first.closed_position.closed_position_id) == (
        first.closed_position
    )
    assert store.list_closed_positions("paper") == [first.closed_position]

    restarted = PortfolioStore(store.path)
    assert restarted.load_closed_position(first.closed_position.closed_position_id) == (
        first.closed_position
    )
    assert restarted.balances("paper") == first.balances


def test_critical_reconciliation_finding_halts_account_without_mutating_history(
    tmp_path,
) -> None:
    store, _, _, command, _ = setup_open_position(tmp_path)
    finding = make_reconciliation_finding(
        account_id="paper",
        kind=ReconciliationKind.EXECUTION_FILL,
        severity=ReconciliationSeverity.CRITICAL,
        source_id=command.command_id,
        found_at=NOW + 2,
        expected={"cumulative_quantity": "2"},
        actual={"cumulative_quantity": "1"},
        evidence={"source": "test"},
    )
    first = store.commit_finding(finding)
    second = store.commit_finding(finding)

    assert first.duplicate is False
    assert second.duplicate is True
    assert first.finding.storage_sequence == 1
    assert store.reconciliation_findings("paper") == [first.finding]
    instance = store.load_instance("paper")
    assert instance is not None
    assert instance.status is AccountStatus.HALTED
    assert instance.halt_reason is not None
    assert command.command_id in instance.halt_reason or "reconciliation" in instance.halt_reason
