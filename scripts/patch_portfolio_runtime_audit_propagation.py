from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


RUNTIME = "src/crypto_quant/portfolio_runtime.py"

replace_once(
    RUNTIME,
    '''    reservations_released: int = 0
    snapshots_created: int = 0
    failures: int = 0
''',
    '''    reservations_released: int = 0
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
                self.health.audit_findings += bootstrap.audit_findings
                self.health.last_cycle_at_ms = current
''',
)
