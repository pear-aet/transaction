from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .portfolio_contracts import (
    ZERO,
    PositionSide,
    as_decimal,
    decimal_string,
)


class ReconciliationSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class ReconciliationKind(StrEnum):
    JOURNAL_BALANCE = "journal_balance"
    EXECUTION_COMMAND = "execution_command"
    EXECUTION_FILL = "execution_fill"
    POSITION_QUANTITY = "position_quantity"
    POSITION_MARGIN = "position_margin"
    RESERVATION = "reservation"
    MARK_PRICE = "mark_price"
    FUNDING = "funding"
    SNAPSHOT = "snapshot"


def _strict_json(value: Any, path: str = "evidence") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError(f"{path} contains a non-finite float")
        return
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{path} contains a non-finite decimal")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            _strict_json(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _strict_json(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains unsupported type: {type(value).__name__}")


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_string(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _stable_identifier(prefix: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        _json_value(payload),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{prefix}:" + hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MarkPriceFact:
    fact_id: str
    account_id: str
    source_event_id: str
    symbol: str
    observed_at: int
    source_sequence: int
    mark_price: Decimal
    storage_sequence: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.fact_id or not self.account_id or not self.source_event_id or not self.symbol:
            raise ValueError("mark fact identifiers must not be empty")
        if min(self.observed_at, self.source_sequence) < 0:
            raise ValueError("mark fact counters must be non-negative")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("mark fact storage_sequence must be positive")
        if self.schema_version <= 0:
            raise ValueError("mark fact schema_version must be positive")
        price = as_decimal(self.mark_price, "mark_price")
        if price <= ZERO:
            raise ValueError("mark_price must be positive")
        object.__setattr__(self, "mark_price", price)
        expected = mark_price_fact_id(
            account_id=self.account_id,
            source_event_id=self.source_event_id,
            symbol=self.symbol,
            observed_at=self.observed_at,
            source_sequence=self.source_sequence,
            mark_price=price,
            schema_version=self.schema_version,
        )
        if self.fact_id != expected:
            raise ValueError("mark fact ID does not match its semantics")

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "account_id": self.account_id,
            "source_event_id": self.source_event_id,
            "symbol": self.symbol,
            "observed_at": self.observed_at,
            "source_sequence": self.source_sequence,
            "mark_price": decimal_string(self.mark_price),
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.semantic_dict(), "storage_sequence": self.storage_sequence}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MarkPriceFact:
        return cls(
            fact_id=str(value["fact_id"]),
            account_id=str(value["account_id"]),
            source_event_id=str(value["source_event_id"]),
            symbol=str(value["symbol"]),
            observed_at=int(value["observed_at"]),
            source_sequence=int(value["source_sequence"]),
            mark_price=as_decimal(value["mark_price"], "mark_price"),
            storage_sequence=(
                None
                if value.get("storage_sequence") is None
                else int(value["storage_sequence"])
            ),
            schema_version=int(value.get("schema_version", 1)),
        )

    def with_storage_sequence(self, sequence: int) -> MarkPriceFact:
        if sequence <= 0:
            raise ValueError("mark fact storage sequence must be positive")
        return replace(self, storage_sequence=sequence)


def mark_price_fact_id(
    *,
    account_id: str,
    source_event_id: str,
    symbol: str,
    observed_at: int,
    source_sequence: int,
    mark_price: Decimal,
    schema_version: int = 1,
) -> str:
    return _stable_identifier(
        "mark_fact",
        {
            "account_id": account_id,
            "source_event_id": source_event_id,
            "symbol": symbol,
            "observed_at": observed_at,
            "source_sequence": source_sequence,
            "mark_price": mark_price,
            "schema_version": schema_version,
        },
    )


def make_mark_price_fact(
    *,
    account_id: str,
    source_event_id: str,
    symbol: str,
    observed_at: int,
    source_sequence: int,
    mark_price: Decimal | int | float | str,
) -> MarkPriceFact:
    selected = as_decimal(mark_price, "mark_price")
    return MarkPriceFact(
        fact_id=mark_price_fact_id(
            account_id=account_id,
            source_event_id=source_event_id,
            symbol=symbol,
            observed_at=observed_at,
            source_sequence=source_sequence,
            mark_price=selected,
        ),
        account_id=account_id,
        source_event_id=source_event_id,
        symbol=symbol,
        observed_at=observed_at,
        source_sequence=source_sequence,
        mark_price=selected,
    )


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    closed_position_id: str
    position_id: str
    account_id: str
    symbol: str
    side: PositionSide
    total_quantity: Decimal
    average_entry_price: Decimal
    average_exit_price: Decimal
    realized_pnl: Decimal
    opening_fees: Decimal
    closing_fees: Decimal
    funding_pnl: Decimal
    open_command_id: str
    open_risk_decision_id: str
    open_intent_id: str
    close_command_id: str
    close_update_id: str
    opened_at: int
    closed_at: int
    storage_sequence: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        identifiers = (
            self.closed_position_id,
            self.position_id,
            self.account_id,
            self.symbol,
            self.open_command_id,
            self.open_risk_decision_id,
            self.open_intent_id,
            self.close_command_id,
            self.close_update_id,
        )
        if any(not value for value in identifiers):
            raise ValueError("closed position identifiers must not be empty")
        if min(self.opened_at, self.closed_at) < 0 or self.closed_at < self.opened_at:
            raise ValueError("invalid closed position chronology")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("closed position storage_sequence must be positive")
        if self.schema_version <= 0:
            raise ValueError("closed position schema_version must be positive")
        for name in (
            "total_quantity",
            "average_entry_price",
            "average_exit_price",
            "realized_pnl",
            "opening_fees",
            "closing_fees",
            "funding_pnl",
        ):
            value = as_decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        if min(self.total_quantity, self.average_entry_price, self.average_exit_price) <= ZERO:
            raise ValueError("closed position quantity and prices must be positive")
        if min(self.opening_fees, self.closing_fees) < ZERO:
            raise ValueError("closed position fees must be non-negative")
        expected = closed_position_id(
            position_id=self.position_id,
            close_command_id=self.close_command_id,
            close_update_id=self.close_update_id,
            closed_at=self.closed_at,
        )
        if self.closed_position_id != expected:
            raise ValueError("closed position ID does not match its identity")

    @property
    def net_pnl(self) -> Decimal:
        return self.realized_pnl + self.funding_pnl - self.opening_fees - self.closing_fees

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "closed_position_id": self.closed_position_id,
            "position_id": self.position_id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "total_quantity": decimal_string(self.total_quantity),
            "average_entry_price": decimal_string(self.average_entry_price),
            "average_exit_price": decimal_string(self.average_exit_price),
            "realized_pnl": decimal_string(self.realized_pnl),
            "opening_fees": decimal_string(self.opening_fees),
            "closing_fees": decimal_string(self.closing_fees),
            "funding_pnl": decimal_string(self.funding_pnl),
            "net_pnl": decimal_string(self.net_pnl),
            "open_command_id": self.open_command_id,
            "open_risk_decision_id": self.open_risk_decision_id,
            "open_intent_id": self.open_intent_id,
            "close_command_id": self.close_command_id,
            "close_update_id": self.close_update_id,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.semantic_dict(), "storage_sequence": self.storage_sequence}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ClosedPosition:
        result = cls(
            closed_position_id=str(value["closed_position_id"]),
            position_id=str(value["position_id"]),
            account_id=str(value["account_id"]),
            symbol=str(value["symbol"]),
            side=PositionSide(value["side"]),
            total_quantity=as_decimal(value["total_quantity"], "total_quantity"),
            average_entry_price=as_decimal(
                value["average_entry_price"], "average_entry_price"
            ),
            average_exit_price=as_decimal(value["average_exit_price"], "average_exit_price"),
            realized_pnl=as_decimal(value["realized_pnl"], "realized_pnl"),
            opening_fees=as_decimal(value["opening_fees"], "opening_fees"),
            closing_fees=as_decimal(value["closing_fees"], "closing_fees"),
            funding_pnl=as_decimal(value["funding_pnl"], "funding_pnl"),
            open_command_id=str(value["open_command_id"]),
            open_risk_decision_id=str(value["open_risk_decision_id"]),
            open_intent_id=str(value["open_intent_id"]),
            close_command_id=str(value["close_command_id"]),
            close_update_id=str(value["close_update_id"]),
            opened_at=int(value["opened_at"]),
            closed_at=int(value["closed_at"]),
            storage_sequence=(
                None
                if value.get("storage_sequence") is None
                else int(value["storage_sequence"])
            ),
            schema_version=int(value.get("schema_version", 1)),
        )
        if "net_pnl" in value and as_decimal(value["net_pnl"], "net_pnl") != result.net_pnl:
            raise ValueError("serialized closed position has inconsistent net_pnl")
        return result

    def with_storage_sequence(self, sequence: int) -> ClosedPosition:
        if sequence <= 0:
            raise ValueError("closed position storage sequence must be positive")
        return replace(self, storage_sequence=sequence)


def closed_position_id(
    *,
    position_id: str,
    close_command_id: str,
    close_update_id: str,
    closed_at: int,
) -> str:
    return _stable_identifier(
        "closed_position",
        {
            "position_id": position_id,
            "close_command_id": close_command_id,
            "close_update_id": close_update_id,
            "closed_at": closed_at,
        },
    )


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    finding_id: str
    account_id: str
    kind: ReconciliationKind
    severity: ReconciliationSeverity
    source_id: str
    found_at: int
    expected: dict[str, Any]
    actual: dict[str, Any]
    evidence: dict[str, Any]
    storage_sequence: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.finding_id or not self.account_id or not self.source_id:
            raise ValueError("reconciliation finding identifiers must not be empty")
        if self.found_at < 0:
            raise ValueError("reconciliation found_at must be non-negative")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("finding storage_sequence must be positive")
        if self.schema_version <= 0:
            raise ValueError("finding schema_version must be positive")
        _strict_json(self.expected, "expected")
        _strict_json(self.actual, "actual")
        _strict_json(self.evidence)
        expected_id = reconciliation_finding_id(
            account_id=self.account_id,
            kind=self.kind,
            severity=self.severity,
            source_id=self.source_id,
            found_at=self.found_at,
            expected=self.expected,
            actual=self.actual,
            evidence=self.evidence,
            schema_version=self.schema_version,
        )
        if self.finding_id != expected_id:
            raise ValueError("reconciliation finding ID does not match its semantics")

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "account_id": self.account_id,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "source_id": self.source_id,
            "found_at": self.found_at,
            "expected": _json_value(self.expected),
            "actual": _json_value(self.actual),
            "evidence": _json_value(self.evidence),
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.semantic_dict(), "storage_sequence": self.storage_sequence}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReconciliationFinding:
        return cls(
            finding_id=str(value["finding_id"]),
            account_id=str(value["account_id"]),
            kind=ReconciliationKind(value["kind"]),
            severity=ReconciliationSeverity(value["severity"]),
            source_id=str(value["source_id"]),
            found_at=int(value["found_at"]),
            expected=dict(value.get("expected") or {}),
            actual=dict(value.get("actual") or {}),
            evidence=dict(value.get("evidence") or {}),
            storage_sequence=(
                None
                if value.get("storage_sequence") is None
                else int(value["storage_sequence"])
            ),
            schema_version=int(value.get("schema_version", 1)),
        )

    def with_storage_sequence(self, sequence: int) -> ReconciliationFinding:
        if sequence <= 0:
            raise ValueError("finding storage sequence must be positive")
        return replace(self, storage_sequence=sequence)


def reconciliation_finding_id(
    *,
    account_id: str,
    kind: ReconciliationKind,
    severity: ReconciliationSeverity,
    source_id: str,
    found_at: int,
    expected: dict[str, Any],
    actual: dict[str, Any],
    evidence: dict[str, Any],
    schema_version: int = 1,
) -> str:
    return _stable_identifier(
        "reconciliation",
        {
            "account_id": account_id,
            "kind": kind.value,
            "severity": severity.value,
            "source_id": source_id,
            "found_at": found_at,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "schema_version": schema_version,
        },
    )


def make_reconciliation_finding(
    *,
    account_id: str,
    kind: ReconciliationKind,
    severity: ReconciliationSeverity,
    source_id: str,
    found_at: int,
    expected: dict[str, Any],
    actual: dict[str, Any],
    evidence: dict[str, Any] | None = None,
) -> ReconciliationFinding:
    selected_evidence = evidence or {}
    return ReconciliationFinding(
        finding_id=reconciliation_finding_id(
            account_id=account_id,
            kind=kind,
            severity=severity,
            source_id=source_id,
            found_at=found_at,
            expected=expected,
            actual=actual,
            evidence=selected_evidence,
        ),
        account_id=account_id,
        kind=kind,
        severity=severity,
        source_id=source_id,
        found_at=found_at,
        expected=expected,
        actual=actual,
        evidence=selected_evidence,
    )
