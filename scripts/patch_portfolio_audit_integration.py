from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


ENGINE = "src/crypto_quant/portfolio_engine.py"

replace_once(
    ENGINE,
    '''from .portfolio_accounting import (
''',
    '''from .portfolio_accounting import (
''',
)

replace_once(
    ENGINE,
    '''from .portfolio_contracts import (
''',
    '''from .portfolio_audit import PortfolioAuditReport, PortfolioAuditor
from .portfolio_contracts import (
''',
)

replace_once(
    ENGINE,
    '''class PortfolioBootstrapResult:
    commands_processed: int
    updates_processed: int
    reservations_released: int
    command_high_watermark: int
    update_high_watermark: int
''',
    '''class PortfolioBootstrapResult:
    commands_processed: int
    updates_processed: int
    reservations_released: int
    command_high_watermark: int
    update_high_watermark: int
    audit_findings: int
''',
)

replace_once(
    ENGINE,
    '''    snapshots_created: int = 0
    barriers: int = 0
''',
    '''    snapshots_created: int = 0
    materialized_audits: int = 0
    materialized_audit_findings: int = 0
    barriers: int = 0
''',
)

replace_once(
    ENGINE,
    '''            "snapshots_created": self.snapshots_created,
            "barriers": self.barriers,
''',
    '''            "snapshots_created": self.snapshots_created,
            "materialized_audits": self.materialized_audits,
            "materialized_audit_findings": self.materialized_audit_findings,
            "barriers": self.barriers,
''',
)

replace_once(
    ENGINE,
    '''        self.correlation_group_provider = correlation_group_provider or (lambda symbol: "default")
        self.health = PortfolioEngineHealth()
''',
    '''        self.correlation_group_provider = correlation_group_provider or (lambda symbol: "default")
        self.health = PortfolioEngineHealth()
        self.auditor = PortfolioAuditor(store, policy)
''',
)

replace_once(
    ENGINE,
    '''    @staticmethod
    def _utc_day(timestamp_ms: int) -> str:
''',
    '''    def audit_materialized_state(
        self,
        *,
        observed_at: int | None = None,
    ) -> PortfolioAuditReport:
        current = int(time.time() * 1000) if observed_at is None else observed_at
        if current < 0:
            raise ValueError("observed_at must be non-negative")
        report = self.auditor.audit(
            self.settings.account_id,
            observed_at=current,
            persist_findings=True,
        )
        self.health.materialized_audits += 1
        self.health.materialized_audit_findings += len(report.findings)
        self.health.reconciliation_failures += report.persisted_findings
        if report.findings:
            self.health.last_error = (
                f"PortfolioAuditError: {len(report.findings)} materialized-state "
                "inconsistencies"
            )
        return report

    @staticmethod
    def _utc_day(timestamp_ms: int) -> str:
''',
)

replace_once(
    ENGINE,
    '''        released = self.release_terminal_reservations(now_ms=current)
        self.reconcile_execution(now_ms=current)
        return PortfolioBootstrapResult(
            commands_processed=commands_processed,
            updates_processed=updates_processed,
            reservations_released=released,
            command_high_watermark=command_after,
            update_high_watermark=update_after,
        )
''',
    '''        released = self.release_terminal_reservations(now_ms=current)
        self.reconcile_execution(now_ms=current)
        audit = self.audit_materialized_state(observed_at=current)
        return PortfolioBootstrapResult(
            commands_processed=commands_processed,
            updates_processed=updates_processed,
            reservations_released=released,
            command_high_watermark=command_after,
            update_high_watermark=update_after,
            audit_findings=len(audit.findings),
        )
''',
)
