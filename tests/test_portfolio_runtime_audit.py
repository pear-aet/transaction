import asyncio
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal

from crypto_quant.execution_store import ExecutionStore
from crypto_quant.portfolio_contracts import AccountHaltReason, AccountPolicy
from crypto_quant.portfolio_engine import PortfolioEngine, PortfolioEngineSettings
from crypto_quant.portfolio_facts import ReconciliationKind
from crypto_quant.portfolio_runtime import PortfolioRuntime, PortfolioRuntimeSettings
from crypto_quant.portfolio_store import PortfolioStore
from crypto_quant.risk_store import RiskStore

from portfolio_fixtures import NOW


def initialized_runtime(tmp_path):
    path = tmp_path / "runtime-audit.db"
    store = PortfolioStore(path)
    engine = PortfolioEngine(
        PortfolioEngineSettings(account_id="paper"),
        AccountPolicy(),
        store=store,
        execution_store=ExecutionStore(path),
        risk_store=RiskStore(path),
        maintenance_rate_provider=lambda symbol, notional: Decimal("0.005"),
    )
    engine.initialize(
        initial_capital="10000",
        source_id="runtime-audit-capital",
        event_time=NOW - 1,
    )
    runtime = PortfolioRuntime(
        engine,
        PortfolioRuntimeSettings(snapshot_interval_ms=60_000),
        clock=lambda: NOW,
    )
    return runtime, store


def test_runtime_forces_new_snapshot_when_audit_finds_drift(tmp_path) -> None:
    runtime, store = initialized_runtime(tmp_path)
    first = asyncio.run(runtime.run_once(now_ms=NOW))
    assert first.bootstrap.reconciliation_findings == 0
    assert first.bootstrap.audit_findings == 0
    assert first.snapshot is not None

    balances = store.balances("paper")
    tampered = replace(
        balances,
        cash=balances.cash - Decimal("1"),
        external_capital=balances.external_capital - Decimal("1"),
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE account_balances SET payload=? WHERE account_id=?",
            (
                json.dumps(tampered.to_dict(), sort_keys=True, separators=(",", ":")),
                "paper",
            ),
        )

    second = asyncio.run(runtime.run_once(now_ms=NOW + 1))

    assert second.bootstrap.commands_processed == 0
    assert second.bootstrap.updates_processed == 0
    assert second.bootstrap.audit_findings >= 1
    assert second.snapshot is not None
    assert second.snapshot.global_halt is True
    assert AccountHaltReason.RECONCILIATION_FAILURE in second.snapshot.halt_reasons
    assert second.snapshot.snapshot_id != first.snapshot.snapshot_id
    health = runtime.health.snapshot()
    assert health["audit_findings"] >= 1
    assert health["snapshots_created"] == 2


def test_runtime_forces_snapshot_for_execution_reconciliation_finding(tmp_path) -> None:
    runtime, _ = initialized_runtime(tmp_path)
    first = asyncio.run(runtime.run_once(now_ms=NOW))
    assert first.snapshot is not None

    def inject_finding(*, now_ms: int | None = None) -> int:
        found_at = NOW + 1 if now_ms is None else now_ms
        runtime.engine._critical_finding(
            kind=ReconciliationKind.EXECUTION_COMMAND,
            source_id="runtime-injected-command-gap",
            found_at=found_at,
            expected={"command": "present"},
            actual={"command": "missing"},
            evidence={"source": "runtime-audit-test"},
        )
        return 1

    runtime.engine.reconcile_execution = inject_finding  # type: ignore[method-assign]
    second = asyncio.run(runtime.run_once(now_ms=NOW + 1))

    assert second.bootstrap.reconciliation_findings == 1
    assert second.bootstrap.audit_findings == 0
    assert second.snapshot is not None
    assert second.snapshot.global_halt is True
    assert AccountHaltReason.RECONCILIATION_FAILURE in second.snapshot.halt_reasons
    health = runtime.health.snapshot()
    assert health["reconciliation_findings"] == 1
    assert health["snapshots_created"] == 2
