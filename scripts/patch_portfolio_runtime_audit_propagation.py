from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


RUNTIME = "src/crypto_quant/portfolio_runtime.py"
STORE = "src/crypto_quant/portfolio_store.py"
LIFECYCLE_TEST = "tests/test_portfolio_lifecycle.py"

replace_once(
    STORE,
    '''import threading
from dataclasses import dataclass
''',
    '''import threading
from dataclasses import dataclass
from decimal import Decimal
''',
)

replace_once(
    RUNTIME,
    '''    reservations_released: int = 0
    snapshots_created: int = 0
    failures: int = 0
''',
    '''    reservations_released: int = 0
    reconciliation_findings: int = 0
    audit_findings: int = 0
    snapshots_created: int = 0
    failures: int = 0
''',
)
replace_once(
    RUNTIME,
    '''            "reservations_released": self.reservations_released,
            "snapshots_created": self.snapshots_created,
''',
    '''            "reservations_released": self.reservations_released,
            "reconciliation_findings": self.reconciliation_findings,
            "audit_findings": self.audit_findings,
            "snapshots_created": self.snapshots_created,
''',
)
replace_once(
    RUNTIME,
    '''                bootstrap.reservations_released,
            )
''',
    '''                bootstrap.reservations_released,
                bootstrap.reconciliation_findings,
                bootstrap.audit_findings,
            )
''',
)
replace_once(
    RUNTIME,
    '''                self.health.reservations_released += bootstrap.reservations_released
                self.health.last_cycle_at_ms = current
''',
    '''                self.health.reservations_released += bootstrap.reservations_released
                self.health.reconciliation_findings += (
                    bootstrap.reconciliation_findings
                )
                self.health.audit_findings += bootstrap.audit_findings
                self.health.last_cycle_at_ms = current
''',
)

replace_once(
    LIFECYCLE_TEST,
    '''    assert result.position.version == position.version + 1
    assert balances == balances
''',
    '''    assert result.position.version == position.version + 1
''',
)
