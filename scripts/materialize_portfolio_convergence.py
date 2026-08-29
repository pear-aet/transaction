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
    ),
    "src/crypto_quant/portfolio_engine.py": (
        "def _assert_initialized(self, *, require_active: bool = True) -> None:",
        "def audit_materialized_state(",
        "audit_findings=len(audit.findings)",
    ),
    "src/crypto_quant/portfolio_runtime.py": (
        "audit_findings: int = 0",
        "bootstrap.audit_findings",
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
    ".github/workflows/apply-portfolio-master-convergence-once.yml": (
        "actions/upload-artifact@v4",
        "actions/download-artifact@v4",
        "needs: [prepare, validate, security, docker]",
        "sha256sum -c materialized-source.sha256",
        "git add -A",
    ),
    ".github/workflows/ci.yml": (
        "runs-on: ubuntu-24.04",
        "Import smoke test",
        "python -m compileall -q src",
    ),
}

FORBIDDEN_MARKERS: dict[str, tuple[str, ...]] = {
    "src/crypto_quant/backtest.py": ('accounting_mode: str = "legacy"',),
    ".github/workflows/apply-portfolio-master-convergence-once.yml": (
        "set +e",
        "status=$?",
        "ubuntu-latest",
        "|| true",
    ),
    ".github/workflows/ci.yml": ("ubuntu-latest",),
}

FINGERPRINT_ROOTS = (
    "src/crypto_quant",
    "tests",
    "scripts",
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
    if not verify_idempotency:
        return
    first = _fingerprint(repo_root)
    for relative in PATCH_CHAIN:
        _run_patch(repo_root, relative)
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
