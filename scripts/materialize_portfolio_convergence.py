from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

PATCH_CHAIN = (
    "scripts/normalize_portfolio_patch_idempotency.py",
    "scripts/fix_portfolio_lifecycle_patch_indentation.py",
    "scripts/patch_portfolio_lifecycle.py",
    "scripts/patch_portfolio_lifecycle_failures.py",
    "scripts/patch_portfolio_lifecycle_round4.py",
    "scripts/patch_portfolio_execution_events.py",
    "scripts/patch_portfolio_engine_boundaries.py",
    "scripts/patch_portfolio_convergence.py",
    "scripts/patch_portfolio_audit_integration.py",
    "scripts/patch_portfolio_audit_hardening.py",
    "scripts/patch_portfolio_snapshot_halt_reason.py",
    "scripts/patch_portfolio_funding_replay.py",
    "scripts/patch_portfolio_reservation_snapshot_boundaries.py",
    "scripts/patch_portfolio_runtime_audit_propagation.py",
    "scripts/patch_legacy_paper_boundary.py",
    "scripts/patch_research_execution_hardening.py",
    "scripts/patch_research_trading_hardening.py",
    "scripts/patch_research_engine_projection.py",
    "scripts/patch_backtest_accounting_mode.py",
    "scripts/patch_backtest_authoritative_default.py",
)

REQUIRED_MARKERS: dict[str, tuple[str, ...]] = {
    "src/crypto_quant/portfolio_contracts.py": (
        "cumulative_closed_quantity: Decimal = ZERO",
        "funding_pnl: Decimal = ZERO",
        "def total_opened_quantity(self) -> Decimal:",
    ),
    "src/crypto_quant/portfolio_accounting.py": (
        "def apply_funding_to_position(",
        "def apply_mark_fact(",
        "closed_position: ClosedPosition | None = None",
    ),
    "src/crypto_quant/portfolio_store.py": (
        "CREATE TABLE IF NOT EXISTS account_closed_positions",
        "CREATE TABLE IF NOT EXISTS account_execution_command_events",
        "CREATE TABLE IF NOT EXISTS account_execution_update_events",
        "def commit_finding(",
        "def load_funding_commit(",
        "def _assert_entry_reservation_available(",
        "snapshot time moved behind the latest snapshot",
        "updated_at=max(updated_at,?)",
    ),
    "src/crypto_quant/portfolio_engine.py": (
        "def _assert_initialized(self, *, require_active: bool = True) -> None:",
        "def audit_materialized_state(",
        "audit_findings=len(audit.findings)",
        "funding source changed immutable semantics",
        "portfolio snapshot time cannot be in the future",
        "portfolio snapshot time precedes account state",
        "portfolio snapshot time moved backwards",
    ),
    "src/crypto_quant/portfolio_runtime.py": (
        "audit_findings: int = 0",
        "bootstrap.audit_findings",
        "portfolio runtime clock moved backwards",
    ),
    "src/crypto_quant/paper.py": (
        "LEGACY_PAPER_DISABLED",
        "allow_legacy_event_engine: bool = False",
        '"authoritative": False',
    ),
    "src/crypto_quant/cli.py": (
        '"--legacy-event-engine"',
        'sub.add_parser("portfolio-status")',
        '"--accounting-mode"',
    ),
    "src/crypto_quant/backtest.py": (
        "from .research_engine import ResearchEventEngine",
        'accounting_mode: str = "portfolio"',
        "def _event_engine(",
    ),
    "src/crypto_quant/research_engine.py": (
        "class ResearchEventEngine(EventEngine):",
        "deferred_funding = sum(",
    ),
    "src/crypto_quant/research_execution.py": (
        "broker_digest = hashlib.blake2s(",
        "research fill requires positive quantity and quote value",
    ),
    "src/crypto_quant/research_trading.py": (
        "maintenance_rate_provider: Callable[",
        "def _maintenance_rate(",
        "research close fee_rate must be in [0, 0.1]",
    ),
    "tests/portfolio_fixtures.py": (
        'identity_suffix: str = ""',
    ),
    "tests/test_portfolio_engine.py": (
        "def test_funding_source_replay_is_idempotent_and_immutable(",
        "def test_conflicting_entry_reservation_is_rejected_atomically(",
        "def test_future_snapshot_fails_closed_without_positions(",
        "def test_snapshot_cannot_precede_account_state_or_prior_snapshot(",
    ),
    "tests/test_portfolio_lifecycle_store.py": (
        "def test_store_rejects_second_active_entry_reservation(",
    ),
    "tests/test_portfolio_store.py": (
        "def test_snapshot_time_is_monotonic_at_store_boundary(",
        "def test_account_instance_time_never_regresses_across_streams(",
    ),
    "tests/test_portfolio_runtime.py": (
        "def test_runtime_rejects_clock_rollback_before_processing(",
    ),
    ".github/workflows/ci.yml": (
        "runs-on: ubuntu-24.04",
        "Import smoke test",
        "python -m compileall -q src",
        "Produce the exact permanent Portfolio source",
        "find \"$GITHUB_WORKSPACE\"",
    ),
}

FORBIDDEN_MARKERS: dict[str, tuple[str, ...]] = {
    "src/crypto_quant/backtest.py": ('accounting_mode: str = "legacy"',),
    ".github/workflows/ci.yml": (
        "ubuntu-latest",
        "set +e",
        "|| true",
    ),
}

FINGERPRINT_ROOTS = (
    "src/crypto_quant",
    "tests",
    "scripts",
    ".github/workflows",
)


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 1:
        path.write_text(text.replace(old, new), encoding="utf-8")
        return
    if count == 0 and new in text:
        return
    raise SystemExit(
        f"expected one temporal source or existing target in {path}, found {count}"
    )


def _replace_all(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count > 0:
        path.write_text(text.replace(old, new), encoding="utf-8")
        return
    if new in text:
        return
    raise SystemExit(f"temporal monotonicity anchor is missing in {path}")


def _append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


def _apply_temporal_hardening(repo_root: Path) -> None:
    store = repo_root / "src/crypto_quant/portfolio_store.py"
    engine = repo_root / "src/crypto_quant/portfolio_engine.py"
    runtime = repo_root / "src/crypto_quant/portfolio_runtime.py"
    store_test = repo_root / "tests/test_portfolio_store.py"
    engine_test = repo_root / "tests/test_portfolio_engine.py"
    runtime_test = repo_root / "tests/test_portfolio_runtime.py"

    _replace_all(
        store,
        '"UPDATE account_instances SET updated_at=? WHERE account_id=?"',
        '"UPDATE account_instances SET updated_at=max(updated_at,?) WHERE account_id=?"',
    )
    _replace_once(
        store,
        '''                return SnapshotCommit(existing, True)
            positions = self._positions_for_account(connection, snapshot.account_id)
''',
        '''                return SnapshotCommit(existing, True)
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (snapshot.account_id,),
            ).fetchone()
            if instance_row is None:
                raise KeyError(f"account not found: {snapshot.account_id}")
            instance = self._instance_from_row(instance_row)
            if snapshot.observed_at < instance.updated_at:
                raise AccountStoreConflict("snapshot time precedes account state")
            latest_row = connection.execute(
                """SELECT * FROM account_snapshots
                WHERE account_id=? ORDER BY observed_at DESC,storage_sequence DESC
                LIMIT 1""",
                (snapshot.account_id,),
            ).fetchone()
            if latest_row is not None:
                latest = AccountSnapshot.from_dict(
                    json.loads(str(latest_row["payload"]))
                ).with_storage_sequence(int(latest_row["storage_sequence"]))
                if snapshot.observed_at < latest.observed_at:
                    raise AccountStoreConflict(
                        "snapshot time moved behind the latest snapshot"
                    )
                if snapshot.observed_at == latest.observed_at:
                    raise AccountStoreConflict(
                        "same-time snapshot changed immutable account semantics"
                    )
            positions = self._positions_for_account(connection, snapshot.account_id)
''',
    )

    _replace_once(
        engine,
        '''        if current > wall_now:
            raise PortfolioHealthError("portfolio snapshot time cannot be in the future")
        instance = self.store.load_instance(self.settings.account_id)
        if instance is None:
            raise PortfolioHealthError("portfolio account has not been initialized")
        positions = tuple(self.store.list_positions(self.settings.account_id))
''',
        '''        if current > wall_now:
            raise PortfolioHealthError("portfolio snapshot time cannot be in the future")
        instance = self.store.load_instance(self.settings.account_id)
        if instance is None:
            raise PortfolioHealthError("portfolio account has not been initialized")
        if current < instance.updated_at:
            raise PortfolioHealthError("portfolio snapshot time precedes account state")
        positions = tuple(self.store.list_positions(self.settings.account_id))
''',
    )
    _replace_once(
        engine,
        '''        prior = self.store.latest_snapshot(self.settings.account_id)
        balances = self.store.balances(self.settings.account_id)
''',
        '''        prior = self.store.latest_snapshot(self.settings.account_id)
        if prior is not None and current < prior.observed_at:
            raise PortfolioHealthError("portfolio snapshot time moved backwards")
        balances = self.store.balances(self.settings.account_id)
''',
    )

    _replace_once(
        runtime,
        '''        async with self._cycle_lock:
            try:
                bootstrap = await asyncio.to_thread(
''',
        '''        async with self._cycle_lock:
            try:
                if (
                    self.health.last_cycle_at_ms is not None
                    and current < self.health.last_cycle_at_ms
                ):
                    raise ValueError("portfolio runtime clock moved backwards")
                bootstrap = await asyncio.to_thread(
''',
    )

    _append_once(
        store_test,
        "def test_snapshot_time_is_monotonic_at_store_boundary(",
        '''def test_snapshot_time_is_monotonic_at_store_boundary(tmp_path) -> None:
    store, _, initialized = initialize_store(tmp_path)
    first = build_account_snapshot(
        account_id="paper",
        observed_at=NOW + 2,
        balances=initialized.balances,
        positions=(),
        day_start_equity=initialized.balances.settled_assets,
        peak_equity=initialized.balances.settled_assets,
        consecutive_losses=0,
        watermarks=AccountWatermarks(),
    )
    committed = store.commit_snapshot(first)

    regressed = build_account_snapshot(
        account_id="paper",
        observed_at=NOW + 1,
        balances=initialized.balances,
        positions=(),
        day_start_equity=initialized.balances.settled_assets,
        peak_equity=initialized.balances.settled_assets,
        consecutive_losses=0,
        watermarks=AccountWatermarks(),
    )
    with pytest.raises(AccountStoreConflict, match="latest snapshot"):
        store.commit_snapshot(regressed)

    assert store.latest_snapshot("paper") == committed.snapshot


def test_account_instance_time_never_regresses_across_streams(tmp_path) -> None:
    from crypto_quant.portfolio_accounting import MarkAccountingResult
    from crypto_quant.portfolio_facts import make_mark_price_fact

    store, policy, _ = initialize_store(tmp_path)
    fact = make_mark_price_fact(
        account_id="paper",
        source_event_id="flat-mark-ahead-of-cash-flow",
        symbol="ETH/USDT:USDT",
        observed_at=NOW + 2,
        source_sequence=1,
        mark_price=50,
    )
    store.commit_mark(MarkAccountingResult(fact, None), policy)
    deposit = deposit_transaction(
        account_id="paper",
        amount="1",
        event_time=NOW + 1,
        source_id="deposit-after-later-mark",
        policy=policy,
    )
    store.apply_external_transaction(deposit, policy)

    instance = store.load_instance("paper")
    assert instance is not None
    assert instance.updated_at == NOW + 2
''',
    )

    _append_once(
        engine_test,
        "def test_snapshot_cannot_precede_account_state_or_prior_snapshot(",
        '''def test_snapshot_cannot_precede_account_state_or_prior_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    engine, _, _, _, portfolio_store, _, _ = system(tmp_path)
    monkeypatch.setattr(
        "crypto_quant.portfolio_engine.time.time",
        lambda: (NOW + 100) / 1000,
    )
    engine.apply_mark(
        source_event_id="flat-account-temporal-mark",
        symbol="ETH/USDT:USDT",
        observed_at=NOW + 2,
        source_sequence=1,
        mark_price=50,
    )

    with pytest.raises(PortfolioHealthError, match="precedes account state"):
        engine.snapshot(observed_at=NOW + 1)

    first = engine.snapshot(observed_at=NOW + 3)
    with pytest.raises(PortfolioHealthError, match="moved backwards"):
        engine.snapshot(observed_at=NOW + 2)

    assert portfolio_store.latest_snapshot("paper") == first
''',
    )

    _append_once(
        runtime_test,
        "def test_runtime_rejects_clock_rollback_before_processing(",
        '''def test_runtime_rejects_clock_rollback_before_processing(tmp_path) -> None:
    engine, _, portfolio_store, _ = runtime_system(tmp_path)
    runtime = PortfolioRuntime(
        engine,
        PortfolioRuntimeSettings(snapshot_interval_ms=0),
        clock=lambda: NOW + 3,
    )
    asyncio.run(runtime.run_once())
    journal_count = portfolio_store.metrics("paper")["journal_transactions"]
    cycles = runtime.health.cycles

    with pytest.raises(ValueError, match="clock moved backwards"):
        asyncio.run(runtime.run_once(now_ms=NOW + 2))

    assert runtime.health.cycles == cycles
    assert runtime.health.failures == 1
    assert portfolio_store.metrics("paper")["journal_transactions"] == journal_count
''',
    )


def _run_patch(repo_root: Path, relative: str) -> None:
    script = repo_root / relative
    if not script.is_file():
        raise SystemExit(f"required convergence patch is missing: {relative}")
    print(f"[portfolio-convergence] applying {relative}", flush=True)
    subprocess.run([sys.executable, str(script)], cwd=repo_root, check=True)


def _fingerprint(repo_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative_root in FINGERPRINT_ROOTS:
        root = repo_root / relative_root
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(repo_root).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _verify_markers(repo_root: Path) -> None:
    failures: list[str] = []
    for relative, markers in REQUIRED_MARKERS.items():
        path = repo_root / relative
        if not path.is_file():
            failures.append(f"missing required permanent file: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                failures.append(f"{relative} lacks required marker: {marker!r}")
    for relative, markers in FORBIDDEN_MARKERS.items():
        path = repo_root / relative
        if not path.is_file():
            failures.append(f"missing gate file: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker in text:
                failures.append(f"{relative} retains forbidden marker: {marker!r}")
    if failures:
        raise SystemExit("portfolio convergence verification failed:\n- " + "\n- ".join(failures))


def _validate_patch_chain(repo_root: Path, *, verify_idempotency: bool) -> None:
    for relative in PATCH_CHAIN:
        _run_patch(repo_root, relative)
    _apply_temporal_hardening(repo_root)
    if not verify_idempotency:
        return
    first = _fingerprint(repo_root)
    for relative in PATCH_CHAIN:
        _run_patch(repo_root, relative)
    _apply_temporal_hardening(repo_root)
    second = _fingerprint(repo_root)
    changed = sorted(set(first) | set(second))
    changed = [path for path in changed if first.get(path) != second.get(path)]
    if changed:
        raise SystemExit(
            "portfolio convergence patch chain is not idempotent:\n- "
            + "\n- ".join(changed)
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Materialize and verify the authoritative Portfolio source chain"
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify permanent markers without applying one-shot patches",
    )
    parser.add_argument(
        "--single-pass",
        action="store_true",
        help="skip the second idempotency pass (not allowed in acceptance workflows)",
    )
    args = parser.parse_args()
    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / "pyproject.toml").is_file():
        raise SystemExit(f"not a crypto-quant repository root: {repo_root}")
    if not args.verify_only:
        _validate_patch_chain(repo_root, verify_idempotency=not args.single_pass)
    _verify_markers(repo_root)
    print("[portfolio-convergence] source and gate invariants verified", flush=True)


if __name__ == "__main__":
    main()
