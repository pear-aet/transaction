import json
import sqlite3
from dataclasses import replace
from decimal import Decimal

from crypto_quant.execution_contracts import OrderLegType, OrderState
from crypto_quant.portfolio_accounting import (
    apply_close_fill,
    apply_open_fill,
    initialize_account_transaction,
    plan_command_reservation,
)
from crypto_quant.portfolio_audit import PortfolioAuditor
from crypto_quant.portfolio_contracts import AccountPolicy
from crypto_quant.portfolio_facts import ReconciliationKind
from crypto_quant.portfolio_store import AccountStatus, PortfolioStore

from portfolio_fixtures import NOW, cumulative_update, execution_command, risk_decision


def setup_open_position(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio-audit.db")
    policy = AccountPolicy()
    initialized = store.initialize_account(
        account_id="paper",
        policy=policy,
        transaction=initialize_account_transaction(
            account_id="paper",
            initial_capital="10000",
            event_time=NOW - 1,
            source_id="audit-capital",
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
            update_id="audit-open-fill",
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
    return store, policy, command, committed


def update_payload(path, query: str, payload: dict, parameters: tuple[object, ...]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            query,
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), *parameters),
        )


def test_auditor_rebuilds_balances_and_position_attribution(tmp_path) -> None:
    store, policy, _, opened = setup_open_position(tmp_path)

    report = PortfolioAuditor(store, policy).audit(
        "paper",
        observed_at=NOW + 2,
    )

    assert report.healthy is True
    assert report.persisted_findings == 0
    assert report.rebuilt_balances == store.balances("paper")
    assert report.stored_balances == report.rebuilt_balances
    assert report.active_reservations == 1
    assert report.active_positions == 1
    assert report.closed_positions == 0
    assert opened.position is not None


def test_tampered_balance_cache_halts_account_and_is_idempotent(tmp_path) -> None:
    store, policy, _, _ = setup_open_position(tmp_path)
    balances = store.balances("paper")
    tampered = replace(
        balances,
        cash=balances.cash - Decimal("1"),
        external_capital=balances.external_capital - Decimal("1"),
    )
    update_payload(
        store.path,
        "UPDATE account_balances SET payload=? WHERE account_id=?",
        tampered.to_dict(),
        ("paper",),
    )

    first = PortfolioAuditor(store, policy).audit(
        "paper",
        observed_at=NOW + 2,
    )
    second = PortfolioAuditor(PortfolioStore(store.path), policy).audit(
        "paper",
        observed_at=NOW + 3,
    )

    assert first.healthy is False
    assert first.persisted_findings >= 1
    assert second.persisted_findings == 0
    assert ReconciliationKind.JOURNAL_BALANCE in {
        finding.kind for finding in first.findings
    }
    instance = store.load_instance("paper")
    assert instance is not None
    assert instance.status is AccountStatus.HALTED


def test_reservation_and_position_margin_drift_are_detected(tmp_path) -> None:
    store, policy, command, opened = setup_open_position(tmp_path)
    reservation = store.load_reservation(command.command_id)
    position = opened.position
    assert reservation is not None
    assert position is not None
    assert reservation.fee_reserve_remaining > Decimal("0.01")
    assert position.position_margin > Decimal("0.01")
    tampered_reservation = replace(
        reservation,
        fee_reserve_remaining=reservation.fee_reserve_remaining - Decimal("0.01"),
    )
    tampered_position = replace(
        position,
        position_margin=position.position_margin - Decimal("0.01"),
    )
    update_payload(
        store.path,
        "UPDATE account_reservations SET payload=? WHERE command_id=?",
        tampered_reservation.to_dict(),
        (command.command_id,),
    )
    update_payload(
        store.path,
        "UPDATE account_positions SET payload=? WHERE account_id=? AND symbol=?",
        tampered_position.to_dict(),
        ("paper", position.symbol),
    )

    report = PortfolioAuditor(store, policy).audit(
        "paper",
        observed_at=NOW + 2,
        persist_findings=False,
    )

    kinds = {finding.kind for finding in report.findings}
    assert report.healthy is False
    assert ReconciliationKind.RESERVATION in kinds
    assert ReconciliationKind.POSITION_MARGIN in kinds


def test_closed_position_is_reconstructed_from_open_close_journal(tmp_path) -> None:
    store, policy, command, opened = setup_open_position(tmp_path)
    position = opened.position
    assert position is not None
    protective = next(
        order for order in command.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    quantity = position.quantity
    quote = quantity * Decimal("99")
    fee = policy.quantize_money(quote * Decimal("0.001"))
    result = apply_close_fill(
        command=command,
        order=protective,
        update=cumulative_update(
            protective,
            update_id="audit-close-fill",
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
    closed_commit = store.commit_fill(result, policy)
    closed = closed_commit.closed_position
    assert closed is not None

    healthy = PortfolioAuditor(store, policy).audit(
        "paper",
        observed_at=NOW + 3,
    )
    assert healthy.healthy is True

    tampered = replace(closed, realized_pnl=closed.realized_pnl + Decimal("1"))
    update_payload(
        store.path,
        "UPDATE account_closed_positions SET payload=? WHERE closed_position_id=?",
        tampered.to_dict(),
        (closed.closed_position_id,),
    )
    report = PortfolioAuditor(store, policy).audit(
        "paper",
        observed_at=NOW + 3,
        persist_findings=False,
    )

    assert report.healthy is False
    assert ReconciliationKind.EXECUTION_FILL in {
        finding.kind for finding in report.findings
    }
