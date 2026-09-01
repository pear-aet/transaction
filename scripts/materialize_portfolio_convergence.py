from __future__ import annotations

import argparse
import ast
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
    "scripts/patch_portfolio_available_balance.py",
    "scripts/patch_legacy_paper_boundary.py",
    "scripts/patch_research_execution_hardening.py",
    "scripts/patch_research_trading_hardening.py",
    "scripts/patch_research_engine_projection.py",
    "scripts/patch_backtest_accounting_mode.py",
    "scripts/patch_backtest_authoritative_default.py",
)

FINALIZATION_PATCH = "scripts/patch_portfolio_runtime_audit_propagation.py"

FINGERPRINT_ROOTS = (
    "src/crypto_quant",
    "tests",
    "scripts",
    ".github/workflows",
)

TEXT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "src/crypto_quant/portfolio_store.py": (
        "CREATE TABLE IF NOT EXISTS account_closed_positions",
        "CREATE TABLE IF NOT EXISTS account_execution_command_events",
        "CREATE TABLE IF NOT EXISTS account_execution_update_events",
        "CREATE TABLE IF NOT EXISTS account_reconciliation_findings",
        "CREATE TABLE IF NOT EXISTS account_funding_applications",
    ),
    "src/crypto_quant/portfolio_contracts.py": (
        "return self.balances.cash + self.unrealized_pnl",
    ),
    "src/crypto_quant/portfolio_engine.py": (
        "funding source changed immutable semantics",
        "portfolio snapshot time cannot be in the future",
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
    ),
    ".github/workflows/ci.yml": (
        "runs-on: ubuntu-24.04",
        "Produce the exact permanent Portfolio source",
        "actions/upload-artifact@v4",
        "actions/download-artifact@v4",
        "python -m compileall -q src",
        'find "$GITHUB_WORKSPACE"',
    ),
}

FORBIDDEN_TEXT: dict[str, tuple[str, ...]] = {
    "src/crypto_quant/backtest.py": ('accounting_mode: str = "legacy"',),
    ".github/workflows/ci.yml": (
        "ubuntu-latest",
        "set +e",
        "status=$?",
        "|| true",
    ),
}

ABSENT_PATHS = (
    ".github/workflows/apply-portfolio-audit-once.yml",
    ".github/workflows/apply-portfolio-master-convergence-once.yml",
    ".github/ci-trigger",
)

MODULE_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "src/crypto_quant/portfolio_accounting.py": (
        "apply_open_fill",
        "apply_close_fill",
        "apply_funding_to_position",
        "apply_mark_fact",
        "build_account_snapshot",
    ),
    "src/crypto_quant/portfolio_facts.py": (
        "make_mark_price_fact",
        "make_reconciliation_finding",
    ),
}

CLASS_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("src/crypto_quant/portfolio_contracts.py", "PortfolioPosition"): (
        "cumulative_closed_quantity",
        "cumulative_close_quote_value",
        "realized_pnl",
        "opening_fees",
        "closing_fees",
        "funding_pnl",
        "last_execution_time",
        "last_mark_time",
        "last_funding_time",
    ),
    ("src/crypto_quant/portfolio_contracts.py", "AccountSnapshot"): (
        "day_start_equity",
        "peak_equity",
        "watermarks",
        "halt_reasons",
    ),
}

CLASS_METHODS: dict[tuple[str, str], tuple[str, ...]] = {
    ("src/crypto_quant/portfolio_contracts.py", "AccountSnapshot"): (
        "available_balance",
        "to_risk_snapshot",
    ),
    ("src/crypto_quant/portfolio_store.py", "PortfolioStore"): (
        "commit_execution_fill",
        "commit_funding",
        "load_funding_commit",
        "commit_mark",
        "commit_finding",
        "_assert_entry_reservation_available",
    ),
    ("src/crypto_quant/portfolio_engine.py", "PortfolioEngine"): (
        "_assert_initialized",
        "process_command",
        "process_update",
        "apply_mark",
        "apply_funding",
        "audit_materialized_state",
        "snapshot",
    ),
    ("src/crypto_quant/portfolio_runtime.py", "PortfolioRuntime"): (
        "run_once",
        "run",
        "stop",
    ),
}

TEST_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "tests/test_portfolio_contracts.py": (
        "test_available_balance_includes_cross_margin_unrealized_pnl",
    ),
    "tests/test_portfolio_engine.py": (
        "test_funding_source_replay_is_idempotent_and_immutable",
        "test_conflicting_entry_reservation_is_rejected_atomically",
        "test_future_snapshot_fails_closed_without_positions",
        "test_snapshot_cannot_precede_account_state_or_prior_snapshot",
    ),
    "tests/test_portfolio_lifecycle_store.py": (
        "test_store_rejects_second_active_entry_reservation",
    ),
    "tests/test_portfolio_runtime.py": (
        "test_runtime_rejects_clock_rollback_before_processing",
    ),
}


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


def _changed(first: dict[str, str], second: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in set(first) | set(second)
        if first.get(path) != second.get(path)
    )


def _run_idempotent_phase(
    repo_root: Path,
    scripts: tuple[str, ...],
    *,
    phase_name: str,
    verify_idempotency: bool,
) -> None:
    for relative in scripts:
        _run_patch(repo_root, relative)
    if not verify_idempotency:
        return
    first = _fingerprint(repo_root)
    for relative in scripts:
        _run_patch(repo_root, relative)
    second = _fingerprint(repo_root)
    changed = _changed(first, second)
    if changed:
        raise SystemExit(
            f"{phase_name} is not idempotent:\n- " + "\n- ".join(changed)
        )


def _parse(repo_root: Path, relative: str) -> ast.Module:
    path = repo_root / relative
    if not path.is_file():
        raise SystemExit(f"missing required permanent file: {relative}")
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except SyntaxError as exc:
        raise SystemExit(f"cannot parse permanent Python file {relative}: {exc}") from exc


def _top_level_functions(module: ast.Module) -> set[str]:
    return {
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _class(module: ast.Module, name: str) -> ast.ClassDef | None:
    return next(
        (
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == name
        ),
        None,
    )


def _class_fields(node: ast.ClassDef) -> set[str]:
    return {
        item.target.id
        for item in node.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    }


def _class_methods(node: ast.ClassDef) -> set[str]:
    return {
        item.name
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _verify_structure(repo_root: Path) -> None:
    failures: list[str] = []
    parsed: dict[str, ast.Module] = {}

    for relative, names in MODULE_FUNCTIONS.items():
        module = parsed.setdefault(relative, _parse(repo_root, relative))
        missing = sorted(set(names) - _top_level_functions(module))
        if missing:
            failures.append(f"{relative} lacks functions: {', '.join(missing)}")

    for (relative, class_name), names in CLASS_FIELDS.items():
        module = parsed.setdefault(relative, _parse(repo_root, relative))
        node = _class(module, class_name)
        if node is None:
            failures.append(f"{relative} lacks class {class_name}")
            continue
        missing = sorted(set(names) - _class_fields(node))
        if missing:
            failures.append(
                f"{relative}:{class_name} lacks fields: {', '.join(missing)}"
            )

    for (relative, class_name), names in CLASS_METHODS.items():
        module = parsed.setdefault(relative, _parse(repo_root, relative))
        node = _class(module, class_name)
        if node is None:
            failures.append(f"{relative} lacks class {class_name}")
            continue
        missing = sorted(set(names) - _class_methods(node))
        if missing:
            failures.append(
                f"{relative}:{class_name} lacks methods: {', '.join(missing)}"
            )

    for relative, names in TEST_FUNCTIONS.items():
        module = parsed.setdefault(relative, _parse(repo_root, relative))
        missing = sorted(set(names) - _top_level_functions(module))
        if missing:
            failures.append(f"{relative} lacks tests: {', '.join(missing)}")

    for relative, markers in TEXT_REQUIREMENTS.items():
        path = repo_root / relative
        if not path.is_file():
            failures.append(f"missing required permanent file: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                failures.append(f"{relative} lacks required marker: {marker!r}")

    for relative, markers in FORBIDDEN_TEXT.items():
        path = repo_root / relative
        if not path.is_file():
            failures.append(f"missing gate file: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker in text:
                failures.append(f"{relative} retains forbidden marker: {marker!r}")

    for relative in ABSENT_PATHS:
        if (repo_root / relative).exists():
            failures.append(f"superseded one-shot path remains: {relative}")

    if failures:
        raise SystemExit(
            "portfolio convergence verification failed:\n- " + "\n- ".join(failures)
        )


def _validate_patch_chain(repo_root: Path, *, verify_idempotency: bool) -> None:
    _run_idempotent_phase(
        repo_root,
        CORE_PATCH_CHAIN,
        phase_name="Portfolio core patch chain",
        verify_idempotency=verify_idempotency,
    )
    _run_idempotent_phase(
        repo_root,
        (FINALIZATION_PATCH,),
        phase_name="Portfolio finalization patch",
        verify_idempotency=verify_idempotency,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Materialize and verify the authoritative Portfolio source chain"
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify permanent structure without applying one-shot patches",
    )
    parser.add_argument(
        "--single-pass",
        action="store_true",
        help="skip second-pass idempotency proof (not allowed in acceptance workflows)",
    )
    args = parser.parse_args()
    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / "pyproject.toml").is_file():
        raise SystemExit(f"not a crypto-quant repository root: {repo_root}")
    if not args.verify_only:
        _validate_patch_chain(
            repo_root,
            verify_idempotency=not args.single_pass,
        )
    _verify_structure(repo_root)
    print("[portfolio-convergence] source and gate invariants verified", flush=True)


if __name__ == "__main__":
    main()
