from dataclasses import replace
from decimal import Decimal

import pytest
from portfolio_fixtures import (
    NOW,
    cumulative_update,
    execution_command,
    risk_decision,
)

from crypto_quant.execution_contracts import OrderLegType, OrderState
from crypto_quant.portfolio_accounting import (
    OrderAccountingCursor,
    apply_open_fill,
    build_account_snapshot,
    deposit_transaction,
    initialize_account_transaction,
    plan_command_reservation,
    withdrawal_transaction,
)
from crypto_quant.portfolio_contracts import AccountPolicy, AccountWatermarks
from crypto_quant.portfolio_store import (
    AccountInstanceError,
    AccountStoreConflict,
    PortfolioStore,
)


def initialize_store(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.db")
    policy = AccountPolicy()
    transaction = initialize_account_transaction(
        account_id="paper",
        initial_capital="10000",
        event_time=NOW - 1,
        source_id="initial-capital",
        policy=policy,
    )
    initialized = store.initialize_account(
        account_id="paper",
        policy=policy,
        transaction=transaction,
    )
    return store, policy, initialized


def test_account_initialization_and_external_cash_flow_are_idempotent(tmp_path) -> None:
    store, policy, initialized = initialize_store(tmp_path)
    duplicate = store.initialize_account(
        account_id="paper",
        policy=policy,
        transaction=replace(initialized.transaction, storage_sequence=None),
    )
    assert duplicate.duplicate is True
    assert duplicate.balances == initialized.balances

    deposit = deposit_transaction(
        account_id="paper",
        amount="500",
        event_time=NOW,
        source_id="deposit-1",
        policy=policy,
    )
    first = store.apply_external_transaction(deposit, policy)
    second = store.apply_external_transaction(deposit, policy)
    assert first.duplicate is False
    assert second.duplicate is True
    assert store.balances("paper").cash == Decimal("10500.00000000")
    assert len(store.journal_transactions("paper")) == 2

    with pytest.raises(AccountInstanceError, match="policy"):
        store.initialize_account(
            account_id="paper",
            policy=replace(policy, version="2.0.0"),
            transaction=replace(initialized.transaction, storage_sequence=None),
        )


def test_oversized_withdrawal_rolls_back_without_journal_gap(tmp_path) -> None:
    store, policy, _ = initialize_store(tmp_path)
    before = store.metrics("paper")
    withdrawal = withdrawal_transaction(
        account_id="paper",
        amount="10001",
        event_time=NOW,
        source_id="withdrawal-too-large",
        policy=policy,
    )

    with pytest.raises(ValueError, match="asset balance cannot be negative"):
        store.apply_external_transaction(withdrawal, policy)

    after = store.metrics("paper")
    assert after["journal_transactions"] == before["journal_transactions"]
    assert after["balances"] == before["balances"]
    assert store.load_journal(withdrawal.transaction_id) is None


def test_tampered_journal_identity_is_rejected_before_storage(tmp_path) -> None:
    store, policy, _ = initialize_store(tmp_path)
    deposit = deposit_transaction(
        account_id="paper",
        amount="10",
        event_time=NOW,
        source_id="deposit-tampered",
        policy=policy,
    )

    with pytest.raises(AccountStoreConflict, match="ID does not match"):
        store.apply_external_transaction(replace(deposit, transaction_id="forged"), policy)
    assert store.load_journal("forged") is None


def test_reservation_fill_snapshot_and_restart_round_trip_are_atomic(tmp_path) -> None:
    store, policy, initialized = initialize_store(tmp_path)
    decision = risk_decision()
    command = execution_command(decision)
    plan = plan_command_reservation(
        account_id="paper",
        command=command,
        decision=decision,
        balances=initialized.balances,
        policy=policy,
        now_ms=NOW,
    )
    committed_plan = store.commit_reservation(plan, policy)
    duplicate_plan = store.commit_reservation(plan, policy)
    assert committed_plan.duplicate is False
    assert duplicate_plan.duplicate is True
    assert committed_plan.reservation.storage_sequence == 1
    assert committed_plan.balances.command_margin > 0
    assert committed_plan.balances.command_fee_reserve > 0

    primary = next(order for order in command.orders if order.leg_type is OrderLegType.PRIMARY)
    quantity = Decimal(str(command.quantity))
    quote = quantity * Decimal("100")
    fee = policy.quantize_money(quote * Decimal("0.001"))
    fill = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="stored-open-fill",
            cumulative_quantity=quantity,
            cumulative_quote_value=quote,
            cumulative_fee=fee,
            event_time=NOW + 1,
            state=OrderState.FILLED,
        ),
        cursor=store.load_order_cursor(primary.order_id, primary.command_id),
        reservation=committed_plan.reservation,
        balances=committed_plan.balances,
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
    )
    committed_fill = store.commit_fill(fill, policy)
    duplicate_fill = store.commit_fill(fill, policy)
    assert committed_fill.duplicate is False
    assert duplicate_fill.duplicate is True
    assert committed_fill.position is not None
    assert store.load_position("paper", command.symbol) == committed_fill.position
    assert store.balances("paper") == committed_fill.balances

    equity = committed_fill.balances.settled_assets
    snapshot = build_account_snapshot(
        account_id="paper",
        observed_at=NOW + 2,
        balances=committed_fill.balances,
        positions=(committed_fill.position,),
        day_start_equity=Decimal("10000"),
        peak_equity=max(Decimal("10000"), equity),
        consecutive_losses=0,
        watermarks=AccountWatermarks(1, 1, 0, 0),
    )
    first_snapshot = store.commit_snapshot(snapshot)
    second_snapshot = store.commit_snapshot(snapshot)
    assert first_snapshot.duplicate is False
    assert second_snapshot.duplicate is True
    assert first_snapshot.snapshot.storage_sequence == 1
    assert store.load_snapshot(snapshot.snapshot_id) == first_snapshot.snapshot
    assert store.latest_snapshot("paper") == first_snapshot.snapshot

    restarted = PortfolioStore(store.path)
    assert restarted.balances("paper") == committed_fill.balances
    assert restarted.load_position("paper", command.symbol) == committed_fill.position
    assert restarted.latest_snapshot("paper") == first_snapshot.snapshot


def test_stale_fill_result_cannot_advance_reservation_or_cursor(tmp_path) -> None:
    store, policy, initialized = initialize_store(tmp_path)
    decision = risk_decision()
    command = execution_command(decision)
    plan = store.commit_reservation(
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
    primary = command.orders[0]
    quantity = Decimal(str(command.quantity))
    first_quantity = quantity / 2
    first_quote = first_quantity * Decimal("100")
    first_fee = policy.quantize_money(first_quote * Decimal("0.001"))
    stale_cursor = OrderAccountingCursor(primary.order_id, primary.command_id)
    first = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="fill-version-1",
            cumulative_quantity=first_quantity,
            cumulative_quote_value=first_quote,
            cumulative_fee=first_fee,
            event_time=NOW + 1,
            state=OrderState.PARTIALLY_FILLED,
        ),
        cursor=stale_cursor,
        reservation=plan.reservation,
        balances=plan.balances,
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
    )
    store.commit_fill(first, policy)

    stale_second = apply_open_fill(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="fill-version-stale",
            cumulative_quantity=quantity,
            cumulative_quote_value=quantity * Decimal("101"),
            cumulative_fee=policy.quantize_money(quantity * Decimal("0.101")),
            event_time=NOW + 2,
            state=OrderState.FILLED,
        ),
        cursor=stale_cursor,
        reservation=plan.reservation,
        balances=plan.balances,
        policy=policy,
        maintenance_margin_rate=Decimal("0.005"),
    )
    before = store.metrics("paper")
    with pytest.raises(AccountStoreConflict, match="reservation version"):
        store.commit_fill(stale_second, policy)
    after = store.metrics("paper")
    assert after == before
    assert store.load_journal(stale_second.transaction.transaction_id) is None


def test_snapshot_cannot_claim_unstored_position_or_balances(tmp_path) -> None:
    store, _, initialized = initialize_store(tmp_path)
    snapshot = build_account_snapshot(
        account_id="paper",
        observed_at=NOW,
        balances=replace(
            initialized.balances,
            cash=Decimal("9999"),
            external_capital=Decimal("9999"),
        ),
        positions=(),
        day_start_equity=10000,
        peak_equity=10000,
        consecutive_losses=0,
        watermarks=AccountWatermarks(),
    )

    with pytest.raises(AccountStoreConflict, match="balances differ"):
        store.commit_snapshot(snapshot)
