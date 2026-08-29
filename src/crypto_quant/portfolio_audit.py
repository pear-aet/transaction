from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .portfolio_contracts import (
    ZERO,
    AccountBalances,
    AccountPolicy,
    CommandReservation,
    JournalAccount,
    JournalSourceKind,
    JournalTransaction,
    PortfolioPosition,
    PostingSide,
    ReservationState,
    as_decimal,
    decimal_string,
    journal_transaction_id,
)
from .portfolio_facts import (
    ClosedPosition,
    ReconciliationFinding,
    ReconciliationKind,
    ReconciliationSeverity,
    make_reconciliation_finding,
)
from .portfolio_store import PortfolioStore

_QUANTITY_TOLERANCE = Decimal("0.000000000001")
_PRICE_TOLERANCE = Decimal("0.000000000001")
_AUDITOR_VERSION = "1.0.0"


class PortfolioAuditError(RuntimeError):
    """Raised when the durable account state cannot be inspected safely."""


@dataclass(frozen=True, slots=True)
class _AuditIssue:
    kind: ReconciliationKind
    source_id: str
    expected: dict[str, Any]
    actual: dict[str, Any]
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PortfolioAuditReport:
    account_id: str
    observed_at: int
    state_time: int
    journal_transactions: int
    latest_journal_sequence: int
    stored_balances: AccountBalances | None
    rebuilt_balances: AccountBalances | None
    active_reservations: int
    active_positions: int
    closed_positions: int
    findings: tuple[ReconciliationFinding, ...]
    persisted_findings: int

    @property
    def healthy(self) -> bool:
        return not self.findings


def _json_dict(payload: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PortfolioAuditError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PortfolioAuditError(f"{name} must contain a JSON object")
    return value


def _money_equal(left: Decimal, right: Decimal, policy: AccountPolicy) -> bool:
    return abs(left - right) <= policy.settlement_quantum


def _quantity_equal(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= _QUANTITY_TOLERANCE


def _price_equal(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= _PRICE_TOLERANCE


def _posting_effect(
    transaction: JournalTransaction,
    account: JournalAccount,
) -> Decimal:
    """Return the normal signed balance effect for one journal account."""
    result = ZERO
    normal_debit = account in {
        JournalAccount.CASH,
        JournalAccount.COMMAND_MARGIN,
        JournalAccount.POSITION_MARGIN,
        JournalAccount.COMMAND_FEE_RESERVE,
        JournalAccount.FEES,
    }
    for posting in transaction.postings:
        if posting.account is not account:
            continue
        increase_side = PostingSide.DEBIT if normal_debit else PostingSide.CREDIT
        result += posting.amount if posting.side is increase_side else -posting.amount
    return result


def _evidence_decimal(
    transaction: JournalTransaction,
    key: str,
) -> Decimal:
    if key not in transaction.evidence:
        raise PortfolioAuditError(
            f"journal transaction {transaction.transaction_id} lacks {key} evidence"
        )
    return as_decimal(transaction.evidence[key], key)


class PortfolioAuditor:
    """Rebuild and cross-check all materialized account state without repairing history."""

    def __init__(self, store: PortfolioStore, policy: AccountPolicy) -> None:
        self.store = store
        self.policy = policy
        self.path = Path(store.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _issue(
        issues: list[_AuditIssue],
        *,
        kind: ReconciliationKind,
        source_id: str,
        expected: dict[str, Any],
        actual: dict[str, Any],
        evidence: dict[str, Any] | None = None,
    ) -> None:
        issues.append(
            _AuditIssue(
                kind=kind,
                source_id=source_id,
                expected=expected,
                actual=actual,
                evidence=evidence or {},
            )
        )

    def _read_journal(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        issues: list[_AuditIssue],
    ) -> tuple[list[JournalTransaction], AccountBalances | None, int, int]:
        rows = connection.execute(
            """SELECT * FROM account_journal
            WHERE account_id=? ORDER BY storage_sequence""",
            (account_id,),
        ).fetchall()
        transactions: list[JournalTransaction] = []
        rebuilt: AccountBalances | None = AccountBalances()
        prior_sequence = 0
        prior_event_time = -1
        latest_sequence = 0
        latest_event_time = 0
        for row in rows:
            sequence = int(row["storage_sequence"])
            try:
                transaction = JournalTransaction.from_dict(
                    _json_dict(str(row["payload"]), "account_journal.payload")
                ).with_storage_sequence(sequence)
                expected_id = journal_transaction_id(
                    account_id=transaction.account_id,
                    source_kind=transaction.source_kind,
                    source_id=transaction.source_id,
                    event_time=transaction.event_time,
                    asset=transaction.asset,
                    postings=transaction.postings,
                    evidence=transaction.evidence,
                    schema_version=transaction.schema_version,
                )
                row_identity = (
                    str(row["transaction_id"]),
                    str(row["account_id"]),
                    str(row["source_kind"]),
                    str(row["source_id"]),
                    int(row["event_time"]),
                )
                payload_identity = (
                    transaction.transaction_id,
                    transaction.account_id,
                    transaction.source_kind.value,
                    transaction.source_id,
                    transaction.event_time,
                )
                if transaction.transaction_id != expected_id or row_identity != payload_identity:
                    raise PortfolioAuditError("journal row identity differs from its payload")
                if transaction.asset != self.policy.settlement_asset:
                    raise PortfolioAuditError("journal transaction uses another settlement asset")
                if sequence <= prior_sequence:
                    raise PortfolioAuditError("journal storage sequence did not advance")
                if transaction.event_time < prior_event_time:
                    raise PortfolioAuditError("journal event time moved backwards")
                if rebuilt is not None:
                    rebuilt = rebuilt.apply(transaction)
                transactions.append(transaction)
                prior_sequence = sequence
                prior_event_time = transaction.event_time
                latest_sequence = sequence
                latest_event_time = transaction.event_time
            except Exception as exc:
                rebuilt = None
                self._issue(
                    issues,
                    kind=ReconciliationKind.JOURNAL_BALANCE,
                    source_id=f"journal-row:{sequence}",
                    expected={"valid": True},
                    actual={"valid": False, "error": str(exc)},
                    evidence={"storage_sequence": sequence},
                )
        return transactions, rebuilt, latest_sequence, latest_event_time

    def _read_balances(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        rebuilt: AccountBalances | None,
        latest_sequence: int,
        latest_event_time: int,
        issues: list[_AuditIssue],
    ) -> tuple[AccountBalances | None, int]:
        row = connection.execute(
            "SELECT * FROM account_balances WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if row is None:
            self._issue(
                issues,
                kind=ReconciliationKind.JOURNAL_BALANCE,
                source_id="balance-cache:missing",
                expected={"present": True},
                actual={"present": False},
            )
            return None, latest_event_time
        updated_at = int(row["updated_at"])
        try:
            stored = AccountBalances.from_dict(
                _json_dict(str(row["payload"]), "account_balances.payload")
            )
        except Exception as exc:
            self._issue(
                issues,
                kind=ReconciliationKind.JOURNAL_BALANCE,
                source_id="balance-cache:invalid",
                expected={"valid": True},
                actual={"valid": False, "error": str(exc)},
            )
            stored = None
        cached_sequence = int(row["journal_sequence"])
        if cached_sequence != latest_sequence:
            self._issue(
                issues,
                kind=ReconciliationKind.JOURNAL_BALANCE,
                source_id="balance-cache:sequence",
                expected={"journal_sequence": latest_sequence},
                actual={"journal_sequence": cached_sequence},
            )
        if latest_sequence and updated_at != latest_event_time:
            self._issue(
                issues,
                kind=ReconciliationKind.JOURNAL_BALANCE,
                source_id="balance-cache:time",
                expected={"updated_at": latest_event_time},
                actual={"updated_at": updated_at},
            )
        if stored is not None and rebuilt is not None and stored != rebuilt:
            self._issue(
                issues,
                kind=ReconciliationKind.JOURNAL_BALANCE,
                source_id="balance-cache:materialized",
                expected=rebuilt.to_dict(),
                actual=stored.to_dict(),
            )
        return stored, max(updated_at, latest_event_time)

    def _read_reservations(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        issues: list[_AuditIssue],
    ) -> tuple[list[CommandReservation], Decimal, Decimal, int]:
        rows = connection.execute(
            """SELECT * FROM account_reservations
            WHERE account_id=? ORDER BY storage_sequence""",
            (account_id,),
        ).fetchall()
        active: list[CommandReservation] = []
        command_margin = ZERO
        fee_reserve = ZERO
        state_time = 0
        for row in rows:
            sequence = int(row["storage_sequence"])
            try:
                reservation = CommandReservation.from_dict(
                    _json_dict(str(row["payload"]), "account_reservations.payload")
                ).with_storage_sequence(sequence)
                row_identity = (
                    str(row["reservation_id"]),
                    str(row["account_id"]),
                    str(row["command_id"]),
                    str(row["source_decision_id"]),
                    str(row["symbol"]),
                    str(row["state"]),
                    int(row["version"]),
                )
                payload_identity = (
                    reservation.reservation_id,
                    reservation.account_id,
                    reservation.command_id,
                    reservation.source_decision_id,
                    reservation.symbol,
                    reservation.state.value,
                    reservation.version,
                )
                if row_identity != payload_identity:
                    raise PortfolioAuditError(
                        "reservation row identity differs from its payload"
                    )
                state_time = max(state_time, reservation.updated_at)
                if reservation.state is ReservationState.ACTIVE:
                    active.append(reservation)
                    command_margin += reservation.command_margin_remaining
                    fee_reserve += reservation.fee_reserve_remaining
            except Exception as exc:
                self._issue(
                    issues,
                    kind=ReconciliationKind.RESERVATION,
                    source_id=f"reservation-row:{sequence}",
                    expected={"valid": True},
                    actual={"valid": False, "error": str(exc)},
                    evidence={"storage_sequence": sequence},
                )
        return active, command_margin, fee_reserve, state_time

    def _read_positions(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        issues: list[_AuditIssue],
    ) -> tuple[list[PortfolioPosition], Decimal, int]:
        rows = connection.execute(
            """SELECT * FROM account_positions
            WHERE account_id=? ORDER BY symbol""",
            (account_id,),
        ).fetchall()
        positions: list[PortfolioPosition] = []
        margin = ZERO
        state_time = 0
        for row in rows:
            try:
                position = PortfolioPosition.from_dict(
                    _json_dict(str(row["payload"]), "account_positions.payload")
                )
                row_identity = (
                    str(row["account_id"]),
                    str(row["symbol"]),
                    str(row["position_id"]),
                    int(row["version"]),
                    int(row["updated_at"]),
                )
                payload_identity = (
                    position.account_id,
                    position.symbol,
                    position.position_id,
                    position.version,
                    position.updated_at,
                )
                if row_identity != payload_identity:
                    raise PortfolioAuditError("position row identity differs from its payload")
                positions.append(position)
                margin += position.position_margin
                state_time = max(state_time, position.updated_at)
            except Exception as exc:
                symbol = str(row["symbol"])
                self._issue(
                    issues,
                    kind=ReconciliationKind.POSITION_QUANTITY,
                    source_id=f"position-row:{symbol}",
                    expected={"valid": True},
                    actual={"valid": False, "error": str(exc)},
                )
        return positions, margin, state_time

    def _read_closed_positions(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        issues: list[_AuditIssue],
    ) -> tuple[list[ClosedPosition], int]:
        rows = connection.execute(
            """SELECT * FROM account_closed_positions
            WHERE account_id=? ORDER BY storage_sequence""",
            (account_id,),
        ).fetchall()
        closed_positions: list[ClosedPosition] = []
        state_time = 0
        for row in rows:
            sequence = int(row["storage_sequence"])
            try:
                closed = ClosedPosition.from_dict(
                    _json_dict(str(row["payload"]), "account_closed_positions.payload")
                ).with_storage_sequence(sequence)
                row_identity = (
                    str(row["closed_position_id"]),
                    str(row["position_id"]),
                    str(row["account_id"]),
                    str(row["symbol"]),
                    str(row["close_update_id"]),
                    int(row["closed_at"]),
                )
                payload_identity = (
                    closed.closed_position_id,
                    closed.position_id,
                    closed.account_id,
                    closed.symbol,
                    closed.close_update_id,
                    closed.closed_at,
                )
                if row_identity != payload_identity:
                    raise PortfolioAuditError(
                        "closed-position row identity differs from its payload"
                    )
                closed_positions.append(closed)
                state_time = max(state_time, closed.closed_at)
            except Exception as exc:
                self._issue(
                    issues,
                    kind=ReconciliationKind.POSITION_QUANTITY,
                    source_id=f"closed-position-row:{sequence}",
                    expected={"valid": True},
                    actual={"valid": False, "error": str(exc)},
                    evidence={"storage_sequence": sequence},
                )
        return closed_positions, state_time

    @staticmethod
    def _transaction_indexes(
        transactions: list[JournalTransaction],
    ) -> tuple[
        dict[str, list[JournalTransaction]],
        dict[str, list[JournalTransaction]],
        dict[str, list[JournalTransaction]],
    ]:
        openings: dict[str, list[JournalTransaction]] = {}
        closings: dict[str, list[JournalTransaction]] = {}
        funding: dict[str, list[JournalTransaction]] = {}
        for transaction in transactions:
            if transaction.source_kind is JournalSourceKind.OPEN_FILL:
                command_id = transaction.evidence.get("command_id")
                if isinstance(command_id, str):
                    openings.setdefault(command_id, []).append(transaction)
            elif transaction.source_kind is JournalSourceKind.CLOSE_FILL:
                position_id = transaction.evidence.get("position_id")
                if isinstance(position_id, str):
                    closings.setdefault(position_id, []).append(transaction)
            elif transaction.source_kind is JournalSourceKind.FUNDING:
                position_id = transaction.evidence.get("position_id")
                if isinstance(position_id, str):
                    funding.setdefault(position_id, []).append(transaction)
        return openings, closings, funding

    def _fill_totals(
        self,
        transactions: list[JournalTransaction],
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        quantity = ZERO
        quote = ZERO
        fees = ZERO
        realized = ZERO
        funding = ZERO
        for transaction in transactions:
            if transaction.source_kind in {
                JournalSourceKind.OPEN_FILL,
                JournalSourceKind.CLOSE_FILL,
            }:
                quantity += _evidence_decimal(transaction, "fill_quantity")
                quote += _evidence_decimal(transaction, "fill_quote_value")
                fees += _posting_effect(transaction, JournalAccount.FEES)
                realized += _posting_effect(transaction, JournalAccount.REALIZED_PNL)
            elif transaction.source_kind is JournalSourceKind.FUNDING:
                funding += _posting_effect(transaction, JournalAccount.FUNDING_PNL)
        return quantity, quote, fees, realized, funding

    def _audit_position_attribution(
        self,
        positions: list[PortfolioPosition],
        closed_positions: list[ClosedPosition],
        transactions: list[JournalTransaction],
        issues: list[_AuditIssue],
    ) -> None:
        openings, closings, funding = self._transaction_indexes(transactions)
        active_ids = {position.position_id for position in positions}
        for position in positions:
            try:
                open_qty, open_quote, open_fees, _, _ = self._fill_totals(
                    openings.get(position.open_command_id, [])
                )
                close_qty, close_quote, close_fees, realized, _ = self._fill_totals(
                    closings.get(position.position_id, [])
                )
                _, _, _, _, funding_pnl = self._fill_totals(
                    funding.get(position.position_id, [])
                )
                expected_quantity = open_qty - close_qty
                expected_average = open_quote / open_qty if open_qty > ZERO else ZERO
                checks = (
                    (
                        "quantity",
                        position.quantity,
                        expected_quantity,
                        _quantity_equal,
                    ),
                    (
                        "total_opened_quantity",
                        position.total_opened_quantity,
                        open_qty,
                        _quantity_equal,
                    ),
                    (
                        "cumulative_closed_quantity",
                        position.cumulative_closed_quantity,
                        close_qty,
                        _quantity_equal,
                    ),
                    (
                        "cumulative_close_quote_value",
                        position.cumulative_close_quote_value,
                        close_quote,
                        lambda left, right: _money_equal(left, right, self.policy),
                    ),
                    (
                        "average_entry_price",
                        position.average_entry_price,
                        expected_average,
                        _price_equal,
                    ),
                    (
                        "opening_fees",
                        position.opening_fees,
                        open_fees,
                        lambda left, right: _money_equal(left, right, self.policy),
                    ),
                    (
                        "closing_fees",
                        position.closing_fees,
                        close_fees,
                        lambda left, right: _money_equal(left, right, self.policy),
                    ),
                    (
                        "realized_pnl",
                        position.realized_pnl,
                        realized,
                        lambda left, right: _money_equal(left, right, self.policy),
                    ),
                    (
                        "funding_pnl",
                        position.funding_pnl,
                        funding_pnl,
                        lambda left, right: _money_equal(left, right, self.policy),
                    ),
                )
                for name, actual, expected, equal in checks:
                    if not equal(actual, expected):
                        self._issue(
                            issues,
                            kind=ReconciliationKind.POSITION_QUANTITY,
                            source_id=f"position:{position.position_id}:{name}",
                            expected={name: decimal_string(expected)},
                            actual={name: decimal_string(actual)},
                            evidence={"symbol": position.symbol},
                        )
            except Exception as exc:
                self._issue(
                    issues,
                    kind=ReconciliationKind.EXECUTION_FILL,
                    source_id=f"position:{position.position_id}:attribution",
                    expected={"reconstructable": True},
                    actual={"reconstructable": False, "error": str(exc)},
                    evidence={"symbol": position.symbol},
                )

        for closed in closed_positions:
            try:
                if closed.position_id in active_ids:
                    raise PortfolioAuditError(
                        "closed position remains present in the active-position table"
                    )
                open_qty, open_quote, open_fees, _, _ = self._fill_totals(
                    openings.get(closed.open_command_id, [])
                )
                close_qty, close_quote, close_fees, realized, _ = self._fill_totals(
                    closings.get(closed.position_id, [])
                )
                _, _, _, _, funding_pnl = self._fill_totals(
                    funding.get(closed.position_id, [])
                )
                average_entry = open_quote / open_qty if open_qty > ZERO else ZERO
                average_exit = close_quote / close_qty if close_qty > ZERO else ZERO
                checks = (
                    (
                        "total_open_quantity",
                        closed.total_quantity,
                        open_qty,
                        _quantity_equal,
                    ),
                    (
                        "total_close_quantity",
                        closed.total_quantity,
                        close_qty,
                        _quantity_equal,
                    ),
                    (
                        "average_entry_price",
                        closed.average_entry_price,
                        average_entry,
                        _price_equal,
                    ),
                    (
                        "average_exit_price",
                        closed.average_exit_price,
                        average_exit,
                        _price_equal,
                    ),
                    (
                        "opening_fees",
                        closed.opening_fees,
                        open_fees,
                        lambda left, right: _money_equal(left, right, self.policy),
                    ),
                    (
                        "closing_fees",
                        closed.closing_fees,
                        close_fees,
                        lambda left, right: _money_equal(left, right, self.policy),
                    ),
                    (
                        "realized_pnl",
                        closed.realized_pnl,
                        realized,
                        lambda left, right: _money_equal(left, right, self.policy),
                    ),
                    (
                        "funding_pnl",
                        closed.funding_pnl,
                        funding_pnl,
                        lambda left, right: _money_equal(left, right, self.policy),
                    ),
                )
                for name, actual, expected, equal in checks:
                    if not equal(actual, expected):
                        self._issue(
                            issues,
                            kind=ReconciliationKind.EXECUTION_FILL,
                            source_id=f"closed:{closed.closed_position_id}:{name}",
                            expected={name: decimal_string(expected)},
                            actual={name: decimal_string(actual)},
                            evidence={"symbol": closed.symbol},
                        )
            except Exception as exc:
                self._issue(
                    issues,
                    kind=ReconciliationKind.EXECUTION_FILL,
                    source_id=f"closed:{closed.closed_position_id}:attribution",
                    expected={"reconstructable": True},
                    actual={"reconstructable": False, "error": str(exc)},
                    evidence={"symbol": closed.symbol},
                )

    def audit(
        self,
        account_id: str,
        *,
        observed_at: int,
        persist_findings: bool = True,
    ) -> PortfolioAuditReport:
        if not account_id or observed_at < 0:
            raise ValueError("invalid Portfolio audit identity or time")
        issues: list[_AuditIssue] = []
        with self._connect() as connection:
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if instance_row is None:
                raise PortfolioAuditError(f"account not found: {account_id}")
            state_time = int(instance_row["updated_at"])
            transactions, rebuilt, latest_sequence, latest_event_time = self._read_journal(
                connection,
                account_id,
                issues,
            )
            stored, balance_time = self._read_balances(
                connection,
                account_id,
                rebuilt,
                latest_sequence,
                latest_event_time,
                issues,
            )
            reservations, reserved_margin, reserved_fees, reservation_time = (
                self._read_reservations(connection, account_id, issues)
            )
            positions, position_margin, position_time = self._read_positions(
                connection,
                account_id,
                issues,
            )
            closed_positions, closed_time = self._read_closed_positions(
                connection,
                account_id,
                issues,
            )
            state_time = max(
                state_time,
                balance_time,
                reservation_time,
                position_time,
                closed_time,
            )

        if stored is not None:
            comparisons = (
                (
                    ReconciliationKind.RESERVATION,
                    "reservation:command-margin",
                    stored.command_margin,
                    reserved_margin,
                ),
                (
                    ReconciliationKind.RESERVATION,
                    "reservation:fee-reserve",
                    stored.command_fee_reserve,
                    reserved_fees,
                ),
                (
                    ReconciliationKind.POSITION_MARGIN,
                    "position:margin",
                    stored.position_margin,
                    position_margin,
                ),
            )
            for kind, source_id, actual, expected in comparisons:
                if not _money_equal(actual, expected, self.policy):
                    self._issue(
                        issues,
                        kind=kind,
                        source_id=source_id,
                        expected={"amount": decimal_string(expected)},
                        actual={"amount": decimal_string(actual)},
                    )

        self._audit_position_attribution(
            positions,
            closed_positions,
            transactions,
            issues,
        )

        findings = tuple(
            make_reconciliation_finding(
                account_id=account_id,
                kind=issue.kind,
                severity=ReconciliationSeverity.CRITICAL,
                source_id=f"materialized-audit:{issue.source_id}",
                found_at=state_time,
                expected=issue.expected,
                actual=issue.actual,
                evidence={
                    "auditor_version": _AUDITOR_VERSION,
                    "latest_journal_sequence": latest_sequence,
                    **issue.evidence,
                },
            )
            for issue in issues
        )
        persisted = 0
        if persist_findings:
            for finding in findings:
                commit = self.store.commit_finding(finding)
                if not commit.duplicate:
                    persisted += 1
        return PortfolioAuditReport(
            account_id=account_id,
            observed_at=observed_at,
            state_time=state_time,
            journal_transactions=len(transactions),
            latest_journal_sequence=latest_sequence,
            stored_balances=stored,
            rebuilt_balances=rebuilt,
            active_reservations=len(reservations),
            active_positions=len(positions),
            closed_positions=len(closed_positions),
            findings=findings,
            persisted_findings=persisted,
        )
