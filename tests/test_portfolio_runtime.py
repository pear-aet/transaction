import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_quant.execution_broker import SimulatedBroker
from crypto_quant.execution_store import ExecutionStore
from crypto_quant.portfolio_contracts import AccountPolicy
from crypto_quant.portfolio_engine import PortfolioEngine, PortfolioEngineSettings
from crypto_quant.portfolio_runtime import (
    PortfolioRiskSnapshotProvider,
    PortfolioRuntime,
    PortfolioRuntimeSettings,
    PortfolioSnapshotUnavailable,
)
from crypto_quant.portfolio_store import PortfolioStore
from crypto_quant.risk_store import RiskStore

from portfolio_fixtures import NOW, execution_command, risk_decision, strategy_intent


def runtime_system(tmp_path):
    path = tmp_path / "runtime.db"
    risk_store = RiskStore(path)
    execution_store = ExecutionStore(path)
    portfolio_store = PortfolioStore(path)
    raw = replace(risk_decision(storage_sequence=1), storage_sequence=None)
    risk_commit = risk_store.commit_evaluation(raw, updated_at=NOW)
    decision = risk_commit.stored_decision.decision
    risk_store.mark_published(decision.decision_id, NOW)
    command = execution_command(decision)
    stored = execution_store.commit_command(command, updated_at=NOW).stored_command
    broker = SimulatedBroker()
    for update in broker.dispatch(stored.command, stored.orders, NOW + 1):
        execution_store.apply_order_update(update)
    execution_store.mark_dispatched(command.command_id, NOW + 1)
    current = execution_store.load_command(command.command_id)
    assert current is not None
    primary = current.orders[0]
    execution_store.apply_order_update(
        broker.fill(
            primary,
            fill_quantity=primary.quantity,
            fill_price=100.1,
            fee=0.1,
            event_time=NOW + 2,
        )
    )
    engine = PortfolioEngine(
        PortfolioEngineSettings(
            "paper",
            execution_engine_instance_ids=frozenset({"execution-primary"}),
        ),
        AccountPolicy(),
        store=portfolio_store,
        execution_store=execution_store,
        risk_store=risk_store,
        maintenance_rate_provider=lambda symbol, notional: Decimal("0.005"),
    )
    engine.initialize(
        initial_capital=10000,
        source_id="runtime-capital",
        event_time=NOW - 1,
    )
    return engine, execution_store, portfolio_store, command


def test_run_once_catches_up_commands_updates_and_persists_snapshot(tmp_path) -> None:
    engine, execution_store, portfolio_store, command = runtime_system(tmp_path)
    runtime = PortfolioRuntime(
        engine,
        PortfolioRuntimeSettings(snapshot_interval_ms=10_000),
        clock=lambda: NOW + 3,
    )

    first = asyncio.run(runtime.run_once())
    assert first.bootstrap.commands_processed == 1
    assert first.bootstrap.updates_processed == execution_store.latest_order_update_sequence()
    assert first.snapshot is not None
    assert portfolio_store.latest_snapshot("paper") == first.snapshot
    assert portfolio_store.load_position("paper", command.symbol) is not None

    second = asyncio.run(runtime.run_once(now_ms=NOW + 4))
    assert second.bootstrap.commands_processed == 0
    assert second.bootstrap.updates_processed == 0
    assert second.snapshot is None
    health = runtime.health.snapshot()
    assert health["cycles"] == 2
    assert health["failures"] == 0
    assert health["last_command_sequence"] == 1
    assert health["last_update_sequence"] == execution_store.latest_order_update_sequence()


def test_runtime_restart_reuses_durable_watermarks_without_duplicate_accounting(
    tmp_path,
) -> None:
    engine, execution_store, portfolio_store, command = runtime_system(tmp_path)
    first_runtime = PortfolioRuntime(
        engine,
        PortfolioRuntimeSettings(snapshot_interval_ms=0),
        clock=lambda: NOW + 3,
    )
    asyncio.run(first_runtime.run_once())
    balances = portfolio_store.balances("paper")
    journal_count = portfolio_store.metrics("paper")["journal_transactions"]

    restarted_engine = PortfolioEngine(
        PortfolioEngineSettings(
            "paper",
            execution_engine_instance_ids=frozenset({"execution-primary"}),
        ),
        AccountPolicy(),
        store=PortfolioStore(portfolio_store.path),
        execution_store=ExecutionStore(execution_store.path),
        risk_store=RiskStore(execution_store.path),
        maintenance_rate_provider=lambda symbol, notional: Decimal("0.005"),
    )
    restarted = PortfolioRuntime(
        restarted_engine,
        PortfolioRuntimeSettings(snapshot_interval_ms=0),
        clock=lambda: NOW + 4,
    )
    cycle = asyncio.run(restarted.run_once())

    assert cycle.bootstrap.commands_processed == 0
    assert cycle.bootstrap.updates_processed == 0
    assert portfolio_store.balances("paper") == balances
    assert portfolio_store.metrics("paper")["journal_transactions"] == journal_count
    assert portfolio_store.load_position("paper", command.symbol) is not None


def test_runtime_loop_stops_cleanly_and_never_runs_concurrent_cycles(tmp_path) -> None:
    engine, _, _, _ = runtime_system(tmp_path)
    tick = NOW + 3

    def clock() -> int:
        nonlocal tick
        tick += 1
        return tick

    runtime = PortfolioRuntime(
        engine,
        PortfolioRuntimeSettings(
            poll_interval_seconds=0.01,
            snapshot_interval_ms=1_000_000,
        ),
        clock=clock,
    )

    async def scenario() -> None:
        task = asyncio.create_task(runtime.run())
        for _ in range(100):
            if runtime.health.cycles >= 2:
                break
            await asyncio.sleep(0.005)
        await runtime.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
    assert runtime.health.running is False
    assert runtime.health.cycles >= 2
    assert runtime.health.failures == 0
    assert runtime.health.stopped_at_ms is not None


def test_risk_snapshot_provider_rejects_missing_future_and_stale_snapshots(tmp_path) -> None:
    engine, _, portfolio_store, _ = runtime_system(tmp_path)
    provider = PortfolioRiskSnapshotProvider(
        portfolio_store,
        "paper",
        max_snapshot_age_ms=10,
    )
    intent = strategy_intent()

    with pytest.raises(PortfolioSnapshotUnavailable, match="unavailable"):
        provider(intent, NOW + 3)

    runtime = PortfolioRuntime(
        engine,
        PortfolioRuntimeSettings(snapshot_interval_ms=0),
        clock=lambda: NOW + 3,
    )
    cycle = asyncio.run(runtime.run_once())
    assert cycle.snapshot is not None
    assert provider(intent, NOW + 3).snapshot_id == cycle.snapshot.snapshot_id

    with pytest.raises(PortfolioSnapshotUnavailable, match="future"):
        provider(intent, NOW + 2)
    with pytest.raises(PortfolioSnapshotUnavailable, match="stale"):
        provider(intent, NOW + 14)


def test_snapshot_provider_returns_halted_authoritative_state_to_risk(tmp_path) -> None:
    engine, _, portfolio_store, _ = runtime_system(tmp_path)
    runtime = PortfolioRuntime(
        engine,
        PortfolioRuntimeSettings(snapshot_interval_ms=0),
        clock=lambda: NOW + 3,
    )
    cycle = asyncio.run(runtime.run_once())
    assert cycle.snapshot is not None
    assert cycle.snapshot.global_halt is True

    provider = PortfolioRiskSnapshotProvider(portfolio_store, "paper")
    risk = provider(strategy_intent(), NOW + 3)
    assert risk.global_halt is True
    assert risk.account_id == "paper"
