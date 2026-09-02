from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_quant.execution import build_execution_command
from crypto_quant.execution_broker import SimulatedBroker
from crypto_quant.execution_contracts import (
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    OrderState,
)
from crypto_quant.execution_store import ExecutionDeliveryState, ExecutionStore
from crypto_quant.portfolio_contracts import AccountHaltReason, AccountPolicy
from crypto_quant.portfolio_engine import (
    PortfolioBarrierError,
    PortfolioEngine,
    PortfolioEngineSettings,
    PortfolioSourceError,
)
from crypto_quant.portfolio_facts import (
    ReconciliationKind,
    ReconciliationSeverity,
    make_reconciliation_finding,
)
from crypto_quant.portfolio_store import PortfolioStore
from crypto_quant.risk_store import RiskStore

from portfolio_fixtures import NOW, execution_command, risk_decision


def persisted_decision(risk_store: RiskStore, *, sequence_hint: int = 1):
    fixture = risk_decision(storage_sequence=sequence_hint)
    raw = replace(fixture, storage_sequence=None)
    commit = risk_store.commit_evaluation(raw, updated_at=NOW)
    decision = commit.stored_decision.decision
    risk_store.mark_published(decision.decision_id, NOW)
    return decision


def system(tmp_path, *, initial_capital: str = "10000"):
    path = tmp_path / "quant.db"
    risk_store = RiskStore(path)
    execution_store = ExecutionStore(path)
    portfolio_store = PortfolioStore(path)
    decision = persisted_decision(risk_store)
    command = execution_command(decision)
    stored_command = execution_store.commit_command(command, updated_at=NOW).stored_command
    policy = AccountPolicy()
    engine = PortfolioEngine(
        PortfolioEngineSettings(
            account_id="paper",
            execution_engine_instance_ids=frozenset({"execution-primary"}),
        ),
        policy,
        store=portfolio_store,
        execution_store=execution_store,
        risk_store=risk_store,
        maintenance_rate_provider=lambda symbol, notional: Decimal("0.005"),
    )
    engine.initialize(
        initial_capital=initial_capital,
        source_id="portfolio-engine-capital",
        event_time=NOW - 1,
    )
    return (
        engine,
        policy,
        risk_store,
        execution_store,
        portfolio_store,
        decision,
        stored_command,
    )


def dispatch_and_fill_primary(execution_store, stored_command, *, fill_price=100.1):
    broker = SimulatedBroker()
    for update in broker.dispatch(
        stored_command.command,
        stored_command.orders,
        NOW + 1,
    ):
        execution_store.apply_order_update(update)
    execution_store.mark_dispatched(stored_command.command.command_id, NOW + 1)
    current = execution_store.load_command(stored_command.command.command_id)
    assert current is not None
    primary = current.orders[0]
    execution_store.apply_order_update(
        broker.fill(
            primary,
            fill_quantity=primary.quantity,
            fill_price=fill_price,
            fee=0.1,
            event_time=NOW + 2,
        )
    )
    return broker


def test_bootstrap_reserves_before_accounting_cumulative_fills(tmp_path) -> None:
    (
        engine,
        _,
        _,
        execution_store,
        portfolio_store,
        _,
        stored_command,
    ) = system(tmp_path)
    dispatch_and_fill_primary(execution_store, stored_command)

    result = engine.bootstrap(now_ms=NOW + 3)
    assert result.commands_processed == 1
    assert result.updates_processed >= 1
    assert portfolio_store.latest_execution_command_sequence("paper") == 1
    assert portfolio_store.latest_execution_update_sequence("paper") == (
        execution_store.latest_order_update_sequence()
    )
    reservation = portfolio_store.load_reservation(stored_command.command.command_id)
    assert reservation is not None
    assert reservation.command_margin_remaining == 0
    position = portfolio_store.load_position("paper", stored_command.command.symbol)
    assert position is not None
    assert position.quantity == Decimal(str(stored_command.command.quantity))
    assert position.open_command_id == stored_command.command.command_id
    assert portfolio_store.balances("paper").position_margin > 0

    duplicate = engine.bootstrap(now_ms=NOW + 4)
    assert duplicate.commands_processed == 0
    assert duplicate.updates_processed == 0
    assert len(portfolio_store.list_positions("paper")) == 1


def test_order_update_cannot_overtake_command_reservation(tmp_path) -> None:
    (
        engine,
        _,
        _,
        execution_store,
        portfolio_store,
        _,
        stored_command,
    ) = system(tmp_path)
    dispatch_and_fill_primary(execution_store, stored_command)
    update = execution_store.load_order_updates(account_id="paper")[0]

    with pytest.raises(PortfolioBarrierError, match="overtook"):
        engine.process_update(update, processed_at=NOW + 3)

    assert portfolio_store.latest_execution_update_sequence("paper") == 0
    assert portfolio_store.load_execution_update_event(
        "paper",
        update.update.update_id,
    ) is None


def test_insufficient_capital_rolls_back_reservation_and_command_event(tmp_path) -> None:
    (
        engine,
        _,
        _,
        _,
        portfolio_store,
        _,
        stored_command,
    ) = system(tmp_path, initial_capital="1")

    with pytest.raises(Exception, match="insufficient cash"):
        engine.process_command(stored_command, processed_at=NOW + 1)

    assert portfolio_store.load_reservation(stored_command.command.command_id) is None
    assert portfolio_store.load_execution_command_event(
        "paper",
        stored_command.command.command_id,
    ) is None
    assert portfolio_store.latest_execution_command_sequence("paper") == 0
    assert portfolio_store.metrics("paper")["journal_transactions"] == 1


def test_persisted_update_semantics_cannot_be_forged(tmp_path) -> None:
    (
        engine,
        _,
        _,
        execution_store,
        _,
        _,
        stored_command,
    ) = system(tmp_path)
    engine.process_command(stored_command, processed_at=NOW + 1)
    dispatch_and_fill_primary(execution_store, stored_command)
    stored_update = execution_store.load_order_updates(account_id="paper")[-1]
    forged = replace(
        stored_update,
        update=replace(
            stored_update.update,
            cumulative_fee=stored_update.update.cumulative_fee + 1,
        ),
    )

    with pytest.raises(PortfolioSourceError, match="differ from storage"):
        engine.process_update(forged, processed_at=NOW + 3)


def test_halted_account_still_accounts_already_persisted_fills(tmp_path) -> None:
    (
        engine,
        _,
        _,
        execution_store,
        portfolio_store,
        _,
        stored_command,
    ) = system(tmp_path)
    engine.process_command(stored_command, processed_at=NOW + 1)
    dispatch_and_fill_primary(execution_store, stored_command)
    finding = make_reconciliation_finding(
        account_id="paper",
        kind=ReconciliationKind.SNAPSHOT,
        severity=ReconciliationSeverity.CRITICAL,
        source_id="pre-existing-account-halt",
        found_at=NOW + 1,
        expected={"healthy": True},
        actual={"healthy": False},
    )
    portfolio_store.commit_finding(finding)

    for update in execution_store.load_order_updates(account_id="paper"):
        engine.process_update(update, processed_at=NOW + 3)

    position = portfolio_store.load_position("paper", stored_command.command.symbol)
    assert position is not None
    snapshot = engine.snapshot(observed_at=NOW + 3)
    assert snapshot.global_halt is True
    assert AccountHaltReason.RECONCILIATION_FAILURE in snapshot.halt_reasons


def test_mark_to_market_snapshot_is_deterministic_and_risk_ready(tmp_path) -> None:
    (
        engine,
        _,
        _,
        execution_store,
        _,
        _,
        stored_command,
    ) = system(tmp_path)
    dispatch_and_fill_primary(execution_store, stored_command)
    engine.bootstrap(now_ms=NOW + 3)

    stale = engine.snapshot(observed_at=NOW + 3)
    assert stale.global_halt is True
    assert AccountHaltReason.STALE_MARK in stale.halt_reasons

    engine.apply_mark(
        source_event_id="portfolio-mark-1",
        symbol=stored_command.command.symbol,
        observed_at=NOW + 4,
        source_sequence=1,
        mark_price=102,
    )
    first = engine.snapshot(observed_at=NOW + 4)
    second = engine.snapshot(observed_at=NOW + 4)
    assert first == second
    assert first.global_halt is False
    assert first.unrealized_pnl != 0
    risk = first.to_risk_snapshot()
    assert risk.account_id == "paper"
    assert risk.positions[0].mark_price == 102
    assert risk.sequence == first.storage_sequence


def test_non_executable_command_advances_command_cursor_without_reservation(tmp_path) -> None:
    path = tmp_path / "quant.db"
    risk_store = RiskStore(path)
    execution_store = ExecutionStore(path)
    portfolio_store = PortfolioStore(path)
    decision = persisted_decision(risk_store)
    policy = ExecutionPolicy(
        mode=ExecutionMode.SIMULATION,
        max_market_age_ms=1,
    )
    command = build_execution_command(
        ExecutionContext(
            "execution-primary",
            decision,
            ExecutionMarketSnapshot(
                "expired-execution-market",
                decision.symbol,
                NOW - 2,
                100,
                bid_price=99.9,
                ask_price=100.1,
            ),
            policy,
            NOW,
        )
    )
    assert command.status is ExecutionCommandStatus.EXPIRED
    stored = execution_store.commit_command(command, updated_at=NOW).stored_command
    engine = PortfolioEngine(
        PortfolioEngineSettings("paper", frozenset({"execution-primary"})),
        AccountPolicy(),
        store=portfolio_store,
        execution_store=execution_store,
        risk_store=risk_store,
        maintenance_rate_provider=lambda symbol, notional: Decimal("0.005"),
    )
    engine.initialize(
        initial_capital=1000,
        source_id="non-executable-capital",
        event_time=NOW - 1,
    )

    result = engine.process_command(stored, processed_at=NOW)
    assert result.disposition == "non_executable"
    assert result.reservation is None
    assert portfolio_store.latest_execution_command_sequence("paper") == 1
    assert portfolio_store.load_reservation(command.command_id) is None
