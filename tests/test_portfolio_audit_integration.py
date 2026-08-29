import json
import sqlite3
from dataclasses import replace
from decimal import Decimal

from crypto_quant.execution_store import ExecutionStore
from crypto_quant.portfolio_contracts import AccountHaltReason, AccountPolicy
from crypto_quant.portfolio_engine import PortfolioEngine, PortfolioEngineSettings
from crypto_quant.portfolio_store import AccountStatus, PortfolioStore
from crypto_quant.risk_store import RiskStore

from portfolio_fixtures import NOW


def initialized_engine(tmp_path):
    path = tmp_path / "portfolio-audit-integration.db"
    store = PortfolioStore(path)
    policy = AccountPolicy()
    engine = PortfolioEngine(
        PortfolioEngineSettings(account_id="paper"),
        policy,
        store=store,
        execution_store=ExecutionStore(path),
        risk_store=RiskStore(path),
        maintenance_rate_provider=lambda symbol, notional: Decimal("0.005"),
    )
    engine.initialize(
        initial_capital="10000",
        source_id="audit-integration-capital",
        event_time=NOW - 1,
    )
    return engine, store


def test_bootstrap_runs_materialized_state_audit(tmp_path) -> None:
    engine, store = initialized_engine(tmp_path)

    result = engine.bootstrap(now_ms=NOW)

    assert result.audit_findings == 0
    health = engine.health_snapshot()["engine"]
    assert health["materialized_audits"] == 1
    assert health["materialized_audit_findings"] == 0
    assert store.reconciliation_findings("paper") == []


def test_bootstrap_halts_before_publishing_healthy_snapshot_on_cache_drift(tmp_path) -> None:
    engine, store = initialized_engine(tmp_path)
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

    result = engine.bootstrap(now_ms=NOW)
    snapshot = engine.snapshot(observed_at=NOW)

    assert result.audit_findings >= 1
    instance = store.load_instance("paper")
    assert instance is not None
    assert instance.status is AccountStatus.HALTED
    assert snapshot.global_halt is True
    assert AccountHaltReason.RECONCILIATION_FAILURE in snapshot.halt_reasons
    health = engine.health_snapshot()["engine"]
    assert health["last_error"] is not None
    assert "reconciliation" in health["last_error"].lower()
