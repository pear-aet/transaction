from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

CORE_PATCH_CHAIN = (
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
    "scripts/patch_legacy_paper_boundary.py",
    "scripts/patch_research_execution_hardening.py",
    "scripts/patch_research_trading_hardening.py",
    "scripts/patch_research_engine_projection.py",
    "scripts/patch_backtest_accounting_mode.py",
    "scripts/patch_backtest_authoritative_default.py",
)

FINALIZATION_PATCH = "scripts/patch_portfolio_runtime_audit_propagation.py"

REQUIRED_MARKERS: dict[str, tuple[str, ...]] = {
    "src/crypto_quant/portfolio_contracts.py": (
        "cumulative_closed_quantity: Decimal = ZERO",
        "funding_pnl: Decimal = ZERO",
        "def total_opened_quantity(self) -> Decimal:",
        "return max(ZERO, self.balances.cash + self.unrealized_pnl)",
    ),
    "src/crypto_quant/portfolio_accounting.py": (
        "def apply_funding_to_position(",
        "transaction: JournalTransaction | None",
        "actual-notional position margin",
        "unplanned_margin: Decimal = ZERO",
        "risk_overrun: Decimal = ZERO",
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
        "now_ms=command.created_at",
        "terminal_times = {",
        "mark.observed_at != position.updated_at",
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
    "tests/portfolio_fixtures.py": ('identity_suffix: str = ""',),
    "tests/test_portfolio_accounting.py": (
        "def test_reservation_separates_initial_margin_from_fee_buffer(",
        "def test_open_fill_uses_actual_notional_for_margin_and_risk(",
    ),
    "tests/test_portfolio_contracts.py": (
        "def test_cross_margin_available_balance_includes_unrealized_pnl(",
    ),
    "tests/test_portfolio_engine.py": (
        "def test_funding_source_replay_is_idempotent_and_immutable(",
        "def test_zero_funding_is_persisted_and_replayed_without_journal(",
        "def test_conflicting_entry_reservation_is_rejected_atomically(",
        "def test_future_snapshot_fails_closed_without_positions(",
        "def test_snapshot_cannot_precede_account_state_or_prior_snapshot(",
        "def test_delayed_bootstrap_accounts_earlier_persisted_fills(",
        "def test_reconciliation_findings_are_idempotent_across_cycles(",
        "def test_external_cash_flows_rebase_equity_anchors(",
        "def test_pre_position_mark_cannot_certify_new_exposure(",
    ),
    "tests/test_portfolio_lifecycle.py": (
        "def test_zero_funding_advances_position_without_fake_journal(",
    ),
    "tests/test_portfolio_lifecycle_store.py": (
        "def test_store_rejects_second_active_entry_reservation(",
    ),
    "tests/test_portfolio_store.py": (
        "def test_snapshot_time_is_monotonic_at_store_boundary(",
        "def test_account_instance_time_never_regresses_across_streams(",
        "def test_cross_stream_journal_uses_storage_sequence_not_global_event_time(",
        "def test_halted_account_still_accepts_external_economic_facts(",
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


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    paths = sorted(set(before) | set(after))
    return [path for path in paths if before.get(path) != after.get(path)]


def _run_idempotency_stage(
    repo_root: Path,
    scripts: tuple[str, ...],
    stage_name: str,
) -> None:
    for relative in scripts:
        _run_patch(repo_root, relative)
    first = _fingerprint(repo_root)
    for relative in scripts:
        _run_patch(repo_root, relative)
    second = _fingerprint(repo_root)
    changed = _changed_paths(first, second)
    if changed:
        raise SystemExit(
            f"portfolio {stage_name} is not idempotent:\n- " + "\n- ".join(changed)
        )


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
    if verify_idempotency:
        _run_idempotency_stage(repo_root, CORE_PATCH_CHAIN, "core patch chain")
        _run_idempotency_stage(repo_root, (FINALIZATION_PATCH,), "finalization patch")
    else:
        for relative in CORE_PATCH_CHAIN:
            _run_patch(repo_root, relative)
        _run_patch(repo_root, FINALIZATION_PATCH)


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
        help="skip second-pass idempotency checks (not allowed in acceptance workflows)",
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
