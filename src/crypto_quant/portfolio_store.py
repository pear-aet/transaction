from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .portfolio_accounting import (
    FillAccountingResult,
    OrderAccountingCursor,
    ReservationPlan,
    ReservationRelease,
)
from .portfolio_contracts import (
    AccountBalances,
    AccountPolicy,
    AccountSnapshot,
    CommandReservation,
    JournalSourceKind,
    JournalTransaction,
    PortfolioPosition,
    ReservationState,
    account_policy_hash,
    journal_transaction_id,
)


class AccountStoreConflict(RuntimeError):
    """Raised when immutable account facts reuse an identity with new semantics."""


class AccountInstanceError(RuntimeError):
    """Raised when an account instance changes its pinned policy identity."""


class AccountStatus(StrEnum):
    ACTIVE = "active"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class AccountInstance:
    account_id: str
    policy_id: str
    policy_version: str
    policy_hash: str
    mode: str
    settlement_asset: str
    status: AccountStatus
    halt_reason: str | None
    initialized_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class StoredJournalTransaction:
    transaction: JournalTransaction
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class AccountInitialization:
    instance: AccountInstance
    balances: AccountBalances
    transaction: JournalTransaction
    duplicate: bool


@dataclass(frozen=True, slots=True)
class ReservationCommit:
    reservation: CommandReservation
    balances: AccountBalances
    transaction: JournalTransaction | None
    duplicate: bool


@dataclass(frozen=True, slots=True)
class FillCommit:
    reservation: CommandReservation
    position: PortfolioPosition | None
    cursor: OrderAccountingCursor
    balances: AccountBalances
    transaction: JournalTransaction
    duplicate: bool


@dataclass(frozen=True, slots=True)
class SnapshotCommit:
    snapshot: AccountSnapshot
    duplicate: bool


class PortfolioStore:
    """Balanced journal plus atomic reservations, positions and accounting cursors."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS account_instances (
                    account_id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('shadow','simulation')),
                    settlement_asset TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','halted')),
                    halt_reason TEXT,
                    initialized_at INTEGER NOT NULL CHECK(initialized_at >= 0),
                    updated_at INTEGER NOT NULL CHECK(updated_at >= initialized_at)
                );

                CREATE TABLE IF NOT EXISTS account_balances (
                    account_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    journal_sequence INTEGER NOT NULL CHECK(journal_sequence >= 0),
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );

                CREATE TABLE IF NOT EXISTS account_journal (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    event_time INTEGER NOT NULL CHECK(event_time >= 0),
                    payload TEXT NOT NULL,
                    UNIQUE(account_id, source_kind, source_id),
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );
                CREATE INDEX IF NOT EXISTS account_journal_account_sequence
                    ON account_journal(account_id, storage_sequence);
                CREATE INDEX IF NOT EXISTS account_journal_event_time
                    ON account_journal(account_id, event_time, storage_sequence);

                CREATE TABLE IF NOT EXISTS account_reservations (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    reservation_id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    command_id TEXT NOT NULL UNIQUE,
                    source_decision_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(
                        state IN ('shadow_recorded','active','released','failed')
                    ),
                    version INTEGER NOT NULL CHECK(version > 0),
                    payload TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );
                CREATE INDEX IF NOT EXISTS account_reservations_active
                    ON account_reservations(account_id, state, symbol, storage_sequence);

                CREATE TABLE IF NOT EXISTS account_positions (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    position_id TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL CHECK(version > 0),
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
                    payload TEXT NOT NULL,
                    PRIMARY KEY(account_id, symbol),
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );

                CREATE TABLE IF NOT EXISTS account_order_cursors (
                    order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version >= 0),
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
                    payload TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );
                CREATE INDEX IF NOT EXISTS account_order_cursors_command
                    ON account_order_cursors(command_id, order_id);

                CREATE TABLE IF NOT EXISTS account_snapshots (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    observed_at INTEGER NOT NULL CHECK(observed_at >= 0),
                    payload TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );
                CREATE INDEX IF NOT EXISTS account_snapshots_account_sequence
                    ON account_snapshots(account_id, storage_sequence);

                CREATE TABLE IF NOT EXISTS account_funding_applications (
                    source_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_time INTEGER NOT NULL CHECK(event_time >= 0),
                    transaction_id TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id),
                    FOREIGN KEY(transaction_id)
                        REFERENCES account_journal(transaction_id)
                );

                CREATE TABLE IF NOT EXISTS account_mark_prices (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    observed_at INTEGER NOT NULL CHECK(observed_at >= 0),
                    source_sequence INTEGER NOT NULL CHECK(source_sequence >= 0),
                    mark_price TEXT NOT NULL,
                    PRIMARY KEY(account_id, symbol),
                    UNIQUE(account_id, source_event_id),
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );
                """
            )

    @staticmethod
    def _encode(value: dict[str, Any]) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _instance_from_row(row: sqlite3.Row) -> AccountInstance:
        return AccountInstance(
            account_id=str(row["account_id"]),
            policy_id=str(row["policy_id"]),
            policy_version=str(row["policy_version"]),
            policy_hash=str(row["policy_hash"]),
            mode=str(row["mode"]),
            settlement_asset=str(row["settlement_asset"]),
            status=AccountStatus(row["status"]),
            halt_reason=None if row["halt_reason"] is None else str(row["halt_reason"]),
            initialized_at=int(row["initialized_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def _encode_transaction(transaction: JournalTransaction) -> str:
        value = transaction.to_dict()
        value["storage_sequence"] = None
        return PortfolioStore._encode(value)

    @staticmethod
    def _decode_transaction(payload: str, sequence: int) -> JournalTransaction:
        return JournalTransaction.from_dict(json.loads(payload)).with_storage_sequence(sequence)

    @staticmethod
    def _encode_reservation(reservation: CommandReservation) -> str:
        value = reservation.to_dict()
        value["storage_sequence"] = None
        return PortfolioStore._encode(value)

    @staticmethod
    def _decode_reservation(payload: str, sequence: int) -> CommandReservation:
        return CommandReservation.from_dict(json.loads(payload)).with_storage_sequence(sequence)

    @staticmethod
    def _encode_position(position: PortfolioPosition) -> str:
        return PortfolioStore._encode(position.to_dict())

    @staticmethod
    def _decode_position(payload: str) -> PortfolioPosition:
        return PortfolioPosition.from_dict(json.loads(payload))

    @staticmethod
    def _encode_cursor(cursor: OrderAccountingCursor) -> str:
        return PortfolioStore._encode(cursor.to_dict())

    @staticmethod
    def _decode_cursor(payload: str) -> OrderAccountingCursor:
        return OrderAccountingCursor.from_dict(json.loads(payload))

    @staticmethod
    def _encode_snapshot(snapshot: AccountSnapshot) -> str:
        value = snapshot.to_dict()
        value["storage_sequence"] = None
        return PortfolioStore._encode(value)

    @staticmethod
    def _same_transaction(left: JournalTransaction, right: JournalTransaction) -> bool:
        return left.semantic_dict() == right.semantic_dict()

    @staticmethod
    def _same_reservation(left: CommandReservation, right: CommandReservation) -> bool:
        left_value = left.to_dict()
        right_value = right.to_dict()
        left_value["storage_sequence"] = None
        right_value["storage_sequence"] = None
        return left_value == right_value

    def load_instance(self, account_id: str) -> AccountInstance | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
        return None if row is None else self._instance_from_row(row)

    def _assert_instance(
        self,
        instance: AccountInstance,
        policy: AccountPolicy,
    ) -> None:
        expected = (
            policy.policy_id,
            policy.version,
            account_policy_hash(policy),
            policy.mode.value,
            policy.settlement_asset,
        )
        actual = (
            instance.policy_id,
            instance.policy_version,
            instance.policy_hash,
            instance.mode,
            instance.settlement_asset,
        )
        if actual != expected:
            raise AccountInstanceError(
                "account policy, mode or settlement asset changed; use a new account epoch"
            )

    def _balances(
        self,
        connection: sqlite3.Connection,
        account_id: str,
    ) -> tuple[AccountBalances, int, int]:
        row = connection.execute(
            "SELECT * FROM account_balances WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"account balances not found: {account_id}")
        return (
            AccountBalances.from_dict(json.loads(str(row["payload"]))),
            int(row["journal_sequence"]),
            int(row["updated_at"]),
        )

    def balances(self, account_id: str) -> AccountBalances:
        with self._connect() as connection:
            balances, _, _ = self._balances(connection, account_id)
        return balances

    def _load_journal_by_id(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
    ) -> JournalTransaction | None:
        row = connection.execute(
            "SELECT * FROM account_journal WHERE transaction_id=?",
            (transaction_id,),
        ).fetchone()
        if row is None:
            return None
        return self._decode_transaction(str(row["payload"]), int(row["storage_sequence"]))

    def load_journal(self, transaction_id: str) -> JournalTransaction | None:
        with self._connect() as connection:
            return self._load_journal_by_id(connection, transaction_id)

    def _validate_transaction_identity(self, transaction: JournalTransaction) -> None:
        expected = journal_transaction_id(
            account_id=transaction.account_id,
            source_kind=transaction.source_kind,
            source_id=transaction.source_id,
            event_time=transaction.event_time,
            asset=transaction.asset,
            postings=transaction.postings,
            evidence=transaction.evidence,
            schema_version=transaction.schema_version,
        )
        if transaction.transaction_id != expected:
            raise AccountStoreConflict("journal transaction ID does not match its semantics")
        if transaction.storage_sequence is not None:
            raise ValueError("only unstored journal transactions may be committed")

    def _commit_transaction(
        self,
        connection: sqlite3.Connection,
        transaction: JournalTransaction,
    ) -> tuple[JournalTransaction, AccountBalances, bool]:
        self._validate_transaction_identity(transaction)
        existing = self._load_journal_by_id(connection, transaction.transaction_id)
        if existing is not None:
            if not self._same_transaction(existing, transaction):
                raise AccountStoreConflict(
                    f"journal transaction changed: {transaction.transaction_id}"
                )
            balances, _, _ = self._balances(connection, transaction.account_id)
            return existing, balances, True

        source_row = connection.execute(
            """SELECT * FROM account_journal
            WHERE account_id=? AND source_kind=? AND source_id=?""",
            (
                transaction.account_id,
                transaction.source_kind.value,
                transaction.source_id,
            ),
        ).fetchone()
        if source_row is not None:
            competing = self._decode_transaction(
                str(source_row["payload"]),
                int(source_row["storage_sequence"]),
            )
            raise AccountStoreConflict(
                "one journal source produced multiple transactions: "
                f"{competing.transaction_id} != {transaction.transaction_id}"
            )

        balances, journal_sequence, updated_at = self._balances(
            connection,
            transaction.account_id,
        )
        if transaction.event_time < updated_at:
            raise AccountStoreConflict("journal event time moved behind the account checkpoint")
        next_balances = balances.apply(transaction)
        cursor = connection.execute(
            """INSERT INTO account_journal(
                transaction_id,account_id,source_kind,source_id,event_time,payload
            ) VALUES(?,?,?,?,?,?)""",
            (
                transaction.transaction_id,
                transaction.account_id,
                transaction.source_kind.value,
                transaction.source_id,
                transaction.event_time,
                self._encode_transaction(transaction),
            ),
        )
        sequence = int(cursor.lastrowid)
        if sequence <= journal_sequence:
            raise RuntimeError("journal sequence did not advance")
        connection.execute(
            """UPDATE account_balances SET
                payload=?,journal_sequence=?,updated_at=? WHERE account_id=?""",
            (
                self._encode(next_balances.to_dict()),
                sequence,
                transaction.event_time,
                transaction.account_id,
            ),
        )
        return transaction.with_storage_sequence(sequence), next_balances, False

    def initialize_account(
        self,
        *,
        account_id: str,
        policy: AccountPolicy,
        transaction: JournalTransaction,
    ) -> AccountInitialization:
        if transaction.account_id != account_id:
            raise ValueError("initialization transaction belongs to another account")
        if transaction.source_kind is not JournalSourceKind.ACCOUNT_INITIALIZED:
            raise ValueError("initial account transaction has the wrong source kind")
        policy_hash = account_policy_hash(policy)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if row is not None:
                instance = self._instance_from_row(row)
                self._assert_instance(instance, policy)
                existing = self._load_journal_by_id(connection, transaction.transaction_id)
                if existing is None or not self._same_transaction(existing, transaction):
                    raise AccountStoreConflict(
                        "account already exists with another initialization transaction"
                    )
                balances, _, _ = self._balances(connection, account_id)
                return AccountInitialization(instance, balances, existing, True)

            connection.execute(
                """INSERT INTO account_instances(
                    account_id,policy_id,policy_version,policy_hash,mode,
                    settlement_asset,status,halt_reason,initialized_at,updated_at
                ) VALUES(?,?,?,?,?,?,'active',NULL,?,?)""",
                (
                    account_id,
                    policy.policy_id,
                    policy.version,
                    policy_hash,
                    policy.mode.value,
                    policy.settlement_asset,
                    transaction.event_time,
                    transaction.event_time,
                ),
            )
            connection.execute(
                """INSERT INTO account_balances(
                    account_id,payload,journal_sequence,updated_at
                ) VALUES(?,?,0,?)""",
                (
                    account_id,
                    self._encode(AccountBalances().to_dict()),
                    transaction.event_time,
                ),
            )
            stored, balances, duplicate = self._commit_transaction(connection, transaction)
            if duplicate:
                raise RuntimeError("new account initialization was unexpectedly duplicate")
            instance = AccountInstance(
                account_id=account_id,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                policy_hash=policy_hash,
                mode=policy.mode.value,
                settlement_asset=policy.settlement_asset,
                status=AccountStatus.ACTIVE,
                halt_reason=None,
                initialized_at=transaction.event_time,
                updated_at=transaction.event_time,
            )
        return AccountInitialization(instance, balances, stored, False)

    def apply_external_transaction(
        self,
        transaction: JournalTransaction,
        policy: AccountPolicy,
    ) -> StoredJournalTransaction:
        if transaction.source_kind not in {
            JournalSourceKind.DEPOSIT,
            JournalSourceKind.WITHDRAWAL,
        }:
            raise ValueError("external transaction has an unsupported source kind")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (transaction.account_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"account not found: {transaction.account_id}")
            instance = self._instance_from_row(row)
            self._assert_instance(instance, policy)
            if instance.status is AccountStatus.HALTED:
                raise AccountInstanceError("halted account cannot accept external cash flow")
            stored, _, duplicate = self._commit_transaction(connection, transaction)
            if not duplicate:
                connection.execute(
                    "UPDATE account_instances SET updated_at=? WHERE account_id=?",
                    (transaction.event_time, transaction.account_id),
                )
        return StoredJournalTransaction(stored, duplicate)

    def load_reservation(self, command_id: str) -> CommandReservation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM account_reservations WHERE command_id=?",
                (command_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_reservation(str(row["payload"]), int(row["storage_sequence"]))

    def active_reservations(
        self,
        account_id: str,
        *,
        symbol: str | None = None,
    ) -> list[CommandReservation]:
        query = """SELECT * FROM account_reservations
        WHERE account_id=? AND state='active'
        AND (? IS NULL OR symbol=?) ORDER BY storage_sequence"""
        with self._connect() as connection:
            rows = connection.execute(
                query,
                (account_id, symbol, symbol),
            ).fetchall()
        return [
            self._decode_reservation(str(row["payload"]), int(row["storage_sequence"]))
            for row in rows
        ]

    def commit_reservation(
        self,
        plan: ReservationPlan,
        policy: AccountPolicy,
    ) -> ReservationCommit:
        reservation = plan.reservation
        if reservation.storage_sequence is not None:
            raise ValueError("only unstored reservations may be committed")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (reservation.account_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"account not found: {reservation.account_id}")
            instance = self._instance_from_row(row)
            self._assert_instance(instance, policy)
            if instance.status is AccountStatus.HALTED:
                raise AccountInstanceError("halted account cannot reserve a command")

            existing_row = connection.execute(
                "SELECT * FROM account_reservations WHERE command_id=?",
                (reservation.command_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._decode_reservation(
                    str(existing_row["payload"]),
                    int(existing_row["storage_sequence"]),
                )
                if not self._same_reservation(existing, reservation):
                    raise AccountStoreConflict(
                        f"command reservation changed: {reservation.command_id}"
                    )
                balances, _, _ = self._balances(connection, reservation.account_id)
                stored_transaction = None
                if plan.transaction is not None:
                    stored_transaction = self._load_journal_by_id(
                        connection,
                        plan.transaction.transaction_id,
                    )
                    if stored_transaction is None:
                        raise AccountStoreConflict(
                            "reservation exists without its journal transaction"
                        )
                return ReservationCommit(existing, balances, stored_transaction, True)

            transaction = None
            if plan.transaction is not None:
                if plan.transaction.account_id != reservation.account_id:
                    raise ValueError("reservation transaction belongs to another account")
                transaction, balances, duplicate = self._commit_transaction(
                    connection,
                    plan.transaction,
                )
                if duplicate:
                    raise AccountStoreConflict(
                        "reservation journal exists without its reservation row"
                    )
            else:
                balances, _, _ = self._balances(connection, reservation.account_id)

            cursor = connection.execute(
                """INSERT INTO account_reservations(
                    reservation_id,account_id,command_id,source_decision_id,
                    symbol,state,version,payload
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    reservation.reservation_id,
                    reservation.account_id,
                    reservation.command_id,
                    reservation.source_decision_id,
                    reservation.symbol,
                    reservation.state.value,
                    reservation.version,
                    self._encode_reservation(reservation),
                ),
            )
            sequence = int(cursor.lastrowid)
            stored_reservation = reservation.with_storage_sequence(sequence)
            connection.execute(
                "UPDATE account_instances SET updated_at=? WHERE account_id=?",
                (reservation.updated_at, reservation.account_id),
            )
        return ReservationCommit(stored_reservation, balances, transaction, False)

    def load_position(self, account_id: str, symbol: str) -> PortfolioPosition | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT payload FROM account_positions
                WHERE account_id=? AND symbol=?""",
                (account_id, symbol),
            ).fetchone()
        return None if row is None else self._decode_position(str(row["payload"]))

    def list_positions(self, account_id: str) -> list[PortfolioPosition]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT payload FROM account_positions
                WHERE account_id=? ORDER BY symbol""",
                (account_id,),
            ).fetchall()
        return [self._decode_position(str(row["payload"])) for row in rows]

    def load_order_cursor(self, order_id: str, command_id: str) -> OrderAccountingCursor:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM account_order_cursors WHERE order_id=?",
                (order_id,),
            ).fetchone()
        if row is None:
            return OrderAccountingCursor(order_id=order_id, command_id=command_id)
        cursor = self._decode_cursor(str(row["payload"]))
        if cursor.command_id != command_id:
            raise AccountStoreConflict("order accounting cursor changed command identity")
        return cursor

    def _stored_reservation(
        self,
        connection: sqlite3.Connection,
        command_id: str,
    ) -> CommandReservation:
        row = connection.execute(
            "SELECT * FROM account_reservations WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"command reservation not found: {command_id}")
        return self._decode_reservation(str(row["payload"]), int(row["storage_sequence"]))

    def _stored_position(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        symbol: str,
    ) -> PortfolioPosition | None:
        row = connection.execute(
            """SELECT payload FROM account_positions
            WHERE account_id=? AND symbol=?""",
            (account_id, symbol),
        ).fetchone()
        return None if row is None else self._decode_position(str(row["payload"]))

    def commit_fill(
        self,
        result: FillAccountingResult,
        policy: AccountPolicy,
    ) -> FillCommit:
        reservation = result.reservation
        transaction = result.transaction
        if transaction.account_id != reservation.account_id:
            raise ValueError("fill transaction belongs to another account")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (reservation.account_id,),
            ).fetchone()
            if instance_row is None:
                raise KeyError(f"account not found: {reservation.account_id}")
            instance = self._instance_from_row(instance_row)
            self._assert_instance(instance, policy)
            if instance.status is AccountStatus.HALTED:
                raise AccountInstanceError("halted account cannot accept fills")

            existing_journal = self._load_journal_by_id(
                connection,
                transaction.transaction_id,
            )
            if existing_journal is not None:
                if not self._same_transaction(existing_journal, transaction):
                    raise AccountStoreConflict(
                        f"fill journal changed: {transaction.transaction_id}"
                    )
                current_reservation = self._stored_reservation(
                    connection,
                    reservation.command_id,
                )
                position = self._stored_position(
                    connection,
                    reservation.account_id,
                    reservation.symbol,
                )
                cursor_row = connection.execute(
                    "SELECT payload FROM account_order_cursors WHERE order_id=?",
                    (result.cursor.order_id,),
                ).fetchone()
                if cursor_row is None:
                    raise AccountStoreConflict("fill journal exists without its order cursor")
                cursor = self._decode_cursor(str(cursor_row["payload"]))
                balances, _, _ = self._balances(connection, reservation.account_id)
                return FillCommit(
                    current_reservation,
                    position,
                    cursor,
                    balances,
                    existing_journal,
                    True,
                )

            current_reservation = self._stored_reservation(
                connection,
                reservation.command_id,
            )
            if current_reservation.version + 1 != reservation.version:
                raise AccountStoreConflict("stale or skipped reservation version in fill commit")
            if current_reservation.storage_sequence != reservation.storage_sequence:
                raise AccountStoreConflict("reservation storage identity changed")

            cursor_row = connection.execute(
                "SELECT payload FROM account_order_cursors WHERE order_id=?",
                (result.cursor.order_id,),
            ).fetchone()
            current_cursor = (
                OrderAccountingCursor(
                    order_id=result.cursor.order_id,
                    command_id=result.cursor.command_id,
                )
                if cursor_row is None
                else self._decode_cursor(str(cursor_row["payload"]))
            )
            if current_cursor.version + 1 != result.cursor.version:
                raise AccountStoreConflict("stale or skipped order accounting cursor")
            if current_cursor.command_id != result.cursor.command_id:
                raise AccountStoreConflict("order cursor command identity changed")

            current_position = self._stored_position(
                connection,
                reservation.account_id,
                reservation.symbol,
            )
            if result.position is None:
                if current_position is None:
                    raise AccountStoreConflict("fill attempted to close a missing position")
            elif current_position is None:
                if result.position.version != 1:
                    raise AccountStoreConflict("new position must begin at version one")
            elif current_position.version + 1 != result.position.version:
                raise AccountStoreConflict("stale or skipped position version")

            stored_transaction, balances, duplicate = self._commit_transaction(
                connection,
                transaction,
            )
            if duplicate:
                raise AccountStoreConflict("fill journal became duplicate during locked commit")

            connection.execute(
                """UPDATE account_reservations SET
                    state=?,version=?,payload=? WHERE command_id=?""",
                (
                    reservation.state.value,
                    reservation.version,
                    self._encode_reservation(reservation),
                    reservation.command_id,
                ),
            )
            connection.execute(
                """INSERT INTO account_order_cursors(
                    order_id,account_id,command_id,version,updated_at,payload
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(order_id) DO UPDATE SET
                    version=excluded.version,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload""",
                (
                    result.cursor.order_id,
                    reservation.account_id,
                    result.cursor.command_id,
                    result.cursor.version,
                    result.cursor.updated_at,
                    self._encode_cursor(result.cursor),
                ),
            )
            if result.position is None:
                connection.execute(
                    "DELETE FROM account_positions WHERE account_id=? AND symbol=?",
                    (reservation.account_id, reservation.symbol),
                )
            else:
                connection.execute(
                    """INSERT INTO account_positions(
                        account_id,symbol,position_id,version,updated_at,payload
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(account_id,symbol) DO UPDATE SET
                        position_id=excluded.position_id,
                        version=excluded.version,
                        updated_at=excluded.updated_at,
                        payload=excluded.payload""",
                    (
                        result.position.account_id,
                        result.position.symbol,
                        result.position.position_id,
                        result.position.version,
                        result.position.updated_at,
                        self._encode_position(result.position),
                    ),
                )
            connection.execute(
                "UPDATE account_instances SET updated_at=? WHERE account_id=?",
                (transaction.event_time, reservation.account_id),
            )
        return FillCommit(
            reservation,
            result.position,
            result.cursor,
            balances,
            stored_transaction,
            False,
        )

    def release_reservation(
        self,
        release: ReservationRelease,
        policy: AccountPolicy,
    ) -> ReservationCommit:
        reservation = release.reservation
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (reservation.account_id,),
            ).fetchone()
            if instance_row is None:
                raise KeyError(f"account not found: {reservation.account_id}")
            instance = self._instance_from_row(instance_row)
            self._assert_instance(instance, policy)
            current = self._stored_reservation(connection, reservation.command_id)
            if current.state is ReservationState.RELEASED:
                if reservation.state is not ReservationState.RELEASED:
                    raise AccountStoreConflict("released reservation attempted to reactivate")
                balances, _, _ = self._balances(connection, reservation.account_id)
                stored_transaction = None
                if release.transaction is not None:
                    stored_transaction = self._load_journal_by_id(
                        connection,
                        release.transaction.transaction_id,
                    )
                    if stored_transaction is None:
                        raise AccountStoreConflict(
                            "released reservation is missing its release journal"
                        )
                return ReservationCommit(current, balances, stored_transaction, True)
            if current.version + 1 != reservation.version:
                raise AccountStoreConflict("stale or skipped reservation release version")
            if current.storage_sequence != reservation.storage_sequence:
                raise AccountStoreConflict("reservation storage identity changed")

            transaction = None
            if release.transaction is not None:
                transaction, balances, duplicate = self._commit_transaction(
                    connection,
                    release.transaction,
                )
                if duplicate:
                    raise AccountStoreConflict(
                        "release journal exists before reservation state changed"
                    )
            else:
                balances, _, _ = self._balances(connection, reservation.account_id)
            connection.execute(
                """UPDATE account_reservations SET
                    state=?,version=?,payload=? WHERE command_id=?""",
                (
                    reservation.state.value,
                    reservation.version,
                    self._encode_reservation(reservation),
                    reservation.command_id,
                ),
            )
            connection.execute(
                "UPDATE account_instances SET updated_at=? WHERE account_id=?",
                (reservation.updated_at, reservation.account_id),
            )
        return ReservationCommit(reservation, balances, transaction, False)

    def commit_funding(
        self,
        *,
        account_id: str,
        symbol: str,
        source_id: str,
        transaction: JournalTransaction,
        policy: AccountPolicy,
    ) -> StoredJournalTransaction:
        if transaction.source_kind is not JournalSourceKind.FUNDING:
            raise ValueError("funding transaction has the wrong source kind")
        if transaction.account_id != account_id or transaction.source_id != source_id:
            raise ValueError("funding transaction identity mismatch")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if instance_row is None:
                raise KeyError(f"account not found: {account_id}")
            instance = self._instance_from_row(instance_row)
            self._assert_instance(instance, policy)
            existing = connection.execute(
                "SELECT * FROM account_funding_applications WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if existing is not None:
                stored = self._load_journal_by_id(
                    connection,
                    str(existing["transaction_id"]),
                )
                if stored is None or not self._same_transaction(stored, transaction):
                    raise AccountStoreConflict("funding source changed its transaction")
                return StoredJournalTransaction(stored, True)
            stored, _, duplicate = self._commit_transaction(connection, transaction)
            if duplicate:
                raise AccountStoreConflict("funding journal exists without application row")
            connection.execute(
                """INSERT INTO account_funding_applications(
                    source_id,account_id,symbol,event_time,transaction_id
                ) VALUES(?,?,?,?,?)""",
                (
                    source_id,
                    account_id,
                    symbol,
                    transaction.event_time,
                    transaction.transaction_id,
                ),
            )
            connection.execute(
                "UPDATE account_instances SET updated_at=? WHERE account_id=?",
                (transaction.event_time, account_id),
            )
        return StoredJournalTransaction(stored, False)

    def commit_snapshot(self, snapshot: AccountSnapshot) -> SnapshotCommit:
        if snapshot.storage_sequence is not None:
            raise ValueError("only unstored account snapshots may be committed")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM account_snapshots WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if row is not None:
                existing = AccountSnapshot.from_dict(json.loads(str(row["payload"])))
                existing = existing.with_storage_sequence(int(row["storage_sequence"]))
                if existing.semantic_dict() != snapshot.semantic_dict():
                    raise AccountStoreConflict("account snapshot ID collision")
                return SnapshotCommit(existing, True)
            positions = self.list_positions(snapshot.account_id)
            if [position.to_dict() for position in positions] != [
                position.to_dict() for position in snapshot.positions
            ]:
                raise AccountStoreConflict("snapshot positions differ from account storage")
            balances, _, updated_at = self._balances(connection, snapshot.account_id)
            if balances != snapshot.balances:
                raise AccountStoreConflict("snapshot balances differ from account storage")
            if snapshot.observed_at < updated_at:
                raise AccountStoreConflict("snapshot time precedes account state")
            cursor = connection.execute(
                """INSERT INTO account_snapshots(
                    snapshot_id,account_id,observed_at,payload
                ) VALUES(?,?,?,?)""",
                (
                    snapshot.snapshot_id,
                    snapshot.account_id,
                    snapshot.observed_at,
                    self._encode_snapshot(snapshot),
                ),
            )
            sequence = int(cursor.lastrowid)
        return SnapshotCommit(snapshot.with_storage_sequence(sequence), False)

    def halt_account(self, account_id: str, reason: str, updated_at: int) -> bool:
        if not reason or updated_at < 0:
            raise ValueError("invalid account halt")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"account not found: {account_id}")
            instance = self._instance_from_row(row)
            if instance.status is AccountStatus.HALTED:
                if instance.halt_reason != reason:
                    raise AccountStoreConflict("account halt reason changed")
                return False
            if updated_at < instance.updated_at:
                raise AccountStoreConflict("account halt time moved backwards")
            connection.execute(
                """UPDATE account_instances SET
                    status='halted',halt_reason=?,updated_at=? WHERE account_id=?""",
                (reason, updated_at, account_id),
            )
        return True

    def journal_transactions(
        self,
        account_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[JournalTransaction]:
        if after_sequence < 0 or not 1 <= limit <= 10_000:
            raise ValueError("invalid journal range")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM account_journal
                WHERE account_id=? AND storage_sequence>?
                ORDER BY storage_sequence LIMIT ?""",
                (account_id, after_sequence, limit),
            ).fetchall()
        return [
            self._decode_transaction(str(row["payload"]), int(row["storage_sequence"]))
            for row in rows
        ]

    def metrics(self, account_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            instance = connection.execute(
                "SELECT status FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if instance is None:
                raise KeyError(f"account not found: {account_id}")
            journal = int(
                connection.execute(
                    "SELECT count(*) FROM account_journal WHERE account_id=?",
                    (account_id,),
                ).fetchone()[0]
            )
            positions = int(
                connection.execute(
                    "SELECT count(*) FROM account_positions WHERE account_id=?",
                    (account_id,),
                ).fetchone()[0]
            )
            reservations = int(
                connection.execute(
                    """SELECT count(*) FROM account_reservations
                    WHERE account_id=? AND state='active'""",
                    (account_id,),
                ).fetchone()[0]
            )
            cursors = int(
                connection.execute(
                    "SELECT count(*) FROM account_order_cursors WHERE account_id=?",
                    (account_id,),
                ).fetchone()[0]
            )
            snapshots = int(
                connection.execute(
                    "SELECT count(*) FROM account_snapshots WHERE account_id=?",
                    (account_id,),
                ).fetchone()[0]
            )
        balances = self.balances(account_id)
        return {
            "status": str(instance["status"]),
            "journal_transactions": journal,
            "positions": positions,
            "active_reservations": reservations,
            "order_cursors": cursors,
            "snapshots": snapshots,
            "balances": balances.to_dict(),
        }
