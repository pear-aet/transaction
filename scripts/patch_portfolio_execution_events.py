from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


def append_once(path: str, marker: str, content: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if marker in text:
        return
    file.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


STORE = "src/crypto_quant/portfolio_store.py"
EXECUTION_STORE = "src/crypto_quant/execution_store.py"
ENGINE = "src/crypto_quant/portfolio_engine.py"

replace_once(
    STORE,
    '''from pathlib import Path
from typing import Any

from .portfolio_accounting import (
''',
    '''from pathlib import Path
from typing import Any

from .execution_contracts import ExecutionCommand
from .execution_store import StoredOrderUpdate
from .portfolio_accounting import (
''',
)
replace_once(
    STORE,
    '''class AccountStatus(StrEnum):
    ACTIVE = "active"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class AccountInstance:
''',
    '''class AccountStatus(StrEnum):
    ACTIVE = "active"
    HALTED = "halted"


class StoredExecutionDisposition(StrEnum):
    RESERVED = "reserved"
    NON_EXECUTABLE = "non_executable"
    ACCOUNTED = "accounted"
    NO_ACCOUNTING = "no_accounting"


@dataclass(frozen=True, slots=True)
class StoredExecutionEvent:
    account_id: str
    source_sequence: int
    source_id: str
    command_id: str
    disposition: str
    processed_at: int
    payload: dict[str, Any]
    duplicate: bool = False

    def __post_init__(self) -> None:
        if not self.account_id or not self.source_id or not self.command_id:
            raise ValueError("stored execution event identifiers must not be empty")
        if min(self.source_sequence, self.processed_at) < 0:
            raise ValueError("stored execution event counters must be non-negative")
        if self.source_sequence <= 0:
            raise ValueError("stored execution event sequence must be positive")
        if self.disposition not in {item.value for item in StoredExecutionDisposition}:
            raise ValueError("invalid stored execution disposition")


@dataclass(frozen=True, slots=True)
class AccountInstance:
''',
)
replace_once(
    STORE,
    '''                CREATE TABLE IF NOT EXISTS account_snapshots (
''',
    '''                CREATE TABLE IF NOT EXISTS account_execution_command_events (
                    account_id TEXT NOT NULL,
                    source_sequence INTEGER NOT NULL CHECK(source_sequence > 0),
                    command_id TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK(
                        disposition IN ('reserved','non_executable')
                    ),
                    processed_at INTEGER NOT NULL CHECK(processed_at >= 0),
                    payload TEXT NOT NULL,
                    PRIMARY KEY(account_id, source_sequence),
                    UNIQUE(account_id, command_id),
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );
                CREATE INDEX IF NOT EXISTS account_command_events_id
                    ON account_execution_command_events(account_id, command_id);

                CREATE TABLE IF NOT EXISTS account_execution_update_events (
                    account_id TEXT NOT NULL,
                    source_sequence INTEGER NOT NULL CHECK(source_sequence > 0),
                    update_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK(
                        disposition IN ('accounted','no_accounting')
                    ),
                    transaction_id TEXT,
                    processed_at INTEGER NOT NULL CHECK(processed_at >= 0),
                    payload TEXT NOT NULL,
                    PRIMARY KEY(account_id, source_sequence),
                    UNIQUE(account_id, update_id),
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id),
                    FOREIGN KEY(transaction_id) REFERENCES account_journal(transaction_id)
                );
                CREATE INDEX IF NOT EXISTS account_update_events_id
                    ON account_execution_update_events(account_id, update_id);
                CREATE INDEX IF NOT EXISTS account_update_events_command
                    ON account_execution_update_events(account_id, command_id, source_sequence);

                CREATE TABLE IF NOT EXISTS account_snapshots (
''',
)
append_once(
    STORE,
    "    def load_execution_command_event(",
    '''    @staticmethod
    def _event_from_row(row: sqlite3.Row, *, update: bool) -> StoredExecutionEvent:
        source_id = str(row["update_id"] if update else row["command_id"])
        return StoredExecutionEvent(
            account_id=str(row["account_id"]),
            source_sequence=int(row["source_sequence"]),
            source_id=source_id,
            command_id=str(row["command_id"]),
            disposition=str(row["disposition"]),
            processed_at=int(row["processed_at"]),
            payload=dict(json.loads(str(row["payload"]))),
        )

    def load_execution_command_event(
        self,
        account_id: str,
        command_id: str,
    ) -> StoredExecutionEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM account_execution_command_events
                WHERE account_id=? AND command_id=?""",
                (account_id, command_id),
            ).fetchone()
        return None if row is None else self._event_from_row(row, update=False)

    def load_execution_update_event(
        self,
        account_id: str,
        update_id: str,
    ) -> StoredExecutionEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM account_execution_update_events
                WHERE account_id=? AND update_id=?""",
                (account_id, update_id),
            ).fetchone()
        return None if row is None else self._event_from_row(row, update=True)

    @staticmethod
    def _command_event_payload(command: ExecutionCommand) -> dict[str, Any]:
        return {
            "storage_sequence": command.storage_sequence,
            "semantic": command.semantic_dict(),
        }

    @staticmethod
    def _update_event_payload(stored_update: StoredOrderUpdate) -> dict[str, Any]:
        return {
            "storage_sequence": stored_update.storage_sequence,
            "prior_state": stored_update.prior_state.value,
            "new_state": stored_update.new_state.value,
            "update": stored_update.update.to_dict(),
        }

    def _insert_command_event(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        source_sequence: int,
        command: ExecutionCommand,
        disposition: StoredExecutionDisposition,
        processed_at: int,
    ) -> StoredExecutionEvent:
        if source_sequence <= 0 or processed_at < 0:
            raise ValueError("invalid command event counters")
        if command.storage_sequence != source_sequence:
            raise AccountStoreConflict("command event sequence differs from command storage")
        payload = self._command_event_payload(command)
        existing = connection.execute(
            """SELECT * FROM account_execution_command_events
            WHERE account_id=? AND (source_sequence=? OR command_id=?)""",
            (account_id, source_sequence, command.command_id),
        ).fetchone()
        if existing is not None:
            event = self._event_from_row(existing, update=False)
            if (
                event.source_sequence != source_sequence
                or event.command_id != command.command_id
                or event.disposition != disposition.value
                or event.payload != payload
            ):
                raise AccountStoreConflict("execution command event changed immutable semantics")
            return StoredExecutionEvent(
                event.account_id,
                event.source_sequence,
                event.source_id,
                event.command_id,
                event.disposition,
                event.processed_at,
                event.payload,
                True,
            )
        connection.execute(
            """INSERT INTO account_execution_command_events(
                account_id,source_sequence,command_id,disposition,processed_at,payload
            ) VALUES(?,?,?,?,?,?)""",
            (
                account_id,
                source_sequence,
                command.command_id,
                disposition.value,
                processed_at,
                self._encode(payload),
            ),
        )
        return StoredExecutionEvent(
            account_id,
            source_sequence,
            command.command_id,
            command.command_id,
            disposition.value,
            processed_at,
            payload,
        )

    def _insert_update_event(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        source_sequence: int,
        stored_update: StoredOrderUpdate,
        command_id: str,
        disposition: StoredExecutionDisposition,
        processed_at: int,
        transaction_id: str | None,
    ) -> StoredExecutionEvent:
        if source_sequence <= 0 or processed_at < 0:
            raise ValueError("invalid update event counters")
        if stored_update.storage_sequence != source_sequence:
            raise AccountStoreConflict("update event sequence differs from update storage")
        if disposition not in {
            StoredExecutionDisposition.ACCOUNTED,
            StoredExecutionDisposition.NO_ACCOUNTING,
        }:
            raise ValueError("invalid update event disposition")
        command_event = connection.execute(
            """SELECT 1 FROM account_execution_command_events
            WHERE account_id=? AND command_id=?""",
            (account_id, command_id),
        ).fetchone()
        if command_event is None:
            raise AccountStoreConflict("order update overtook its command event")
        payload = self._update_event_payload(stored_update)
        existing = connection.execute(
            """SELECT * FROM account_execution_update_events
            WHERE account_id=? AND (source_sequence=? OR update_id=?)""",
            (account_id, source_sequence, stored_update.update.update_id),
        ).fetchone()
        if existing is not None:
            event = self._event_from_row(existing, update=True)
            if (
                event.source_sequence != source_sequence
                or event.source_id != stored_update.update.update_id
                or event.command_id != command_id
                or event.disposition != disposition.value
                or event.payload != payload
                or existing["transaction_id"] != transaction_id
            ):
                raise AccountStoreConflict("execution update event changed immutable semantics")
            return StoredExecutionEvent(
                event.account_id,
                event.source_sequence,
                event.source_id,
                event.command_id,
                event.disposition,
                event.processed_at,
                event.payload,
                True,
            )
        connection.execute(
            """INSERT INTO account_execution_update_events(
                account_id,source_sequence,update_id,command_id,disposition,
                transaction_id,processed_at,payload
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                account_id,
                source_sequence,
                stored_update.update.update_id,
                command_id,
                disposition.value,
                transaction_id,
                processed_at,
                self._encode(payload),
            ),
        )
        return StoredExecutionEvent(
            account_id,
            source_sequence,
            stored_update.update.update_id,
            command_id,
            disposition.value,
            processed_at,
            payload,
        )

    def record_execution_command_disposition(
        self,
        *,
        account_id: str,
        source_sequence: int,
        command: ExecutionCommand,
        disposition: StoredExecutionDisposition,
        processed_at: int,
    ) -> StoredExecutionEvent:
        if disposition is not StoredExecutionDisposition.NON_EXECUTABLE:
            raise ValueError("record-only command disposition must be non_executable")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if instance_row is None:
                raise KeyError(f"account not found: {account_id}")
            return self._insert_command_event(
                connection,
                account_id=account_id,
                source_sequence=source_sequence,
                command=command,
                disposition=disposition,
                processed_at=processed_at,
            )

    def commit_execution_command_reservation(
        self,
        plan: ReservationPlan,
        *,
        policy: AccountPolicy,
        source_sequence: int,
        command: ExecutionCommand,
        processed_at: int,
    ) -> ReservationCommit:
        reservation = plan.reservation
        if reservation.command_id != command.command_id:
            raise ValueError("reservation belongs to another execution command")
        if reservation.storage_sequence is not None:
            raise ValueError("only unstored reservations may be committed")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_event = connection.execute(
                """SELECT * FROM account_execution_command_events
                WHERE account_id=? AND (source_sequence=? OR command_id=?)""",
                (reservation.account_id, source_sequence, command.command_id),
            ).fetchone()
            if existing_event is not None:
                event = self._insert_command_event(
                    connection,
                    account_id=reservation.account_id,
                    source_sequence=source_sequence,
                    command=command,
                    disposition=StoredExecutionDisposition.RESERVED,
                    processed_at=processed_at,
                )
                existing = self._stored_reservation(connection, reservation.command_id)
                balances, _, _ = self._balances(connection, reservation.account_id)
                transaction = None
                if plan.transaction is not None:
                    transaction = self._load_journal_by_id(
                        connection,
                        plan.transaction.transaction_id,
                    )
                    if transaction is None:
                        raise AccountStoreConflict(
                            "command event exists without its reservation journal"
                        )
                return ReservationCommit(existing, balances, transaction, event.duplicate)

            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (reservation.account_id,),
            ).fetchone()
            if instance_row is None:
                raise KeyError(f"account not found: {reservation.account_id}")
            instance = self._instance_from_row(instance_row)
            self._assert_instance(instance, policy)
            if instance.status is AccountStatus.HALTED:
                raise AccountInstanceError("halted account cannot reserve a command")

            existing_row = connection.execute(
                "SELECT * FROM account_reservations WHERE command_id=?",
                (reservation.command_id,),
            ).fetchone()
            if existing_row is not None:
                raise AccountStoreConflict(
                    "reservation exists without its atomic execution command event"
                )

            transaction = None
            if plan.transaction is not None:
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
            stored_reservation = reservation.with_storage_sequence(int(cursor.lastrowid))
            self._insert_command_event(
                connection,
                account_id=reservation.account_id,
                source_sequence=source_sequence,
                command=command,
                disposition=StoredExecutionDisposition.RESERVED,
                processed_at=processed_at,
            )
            connection.execute(
                "UPDATE account_instances SET updated_at=max(updated_at,?) WHERE account_id=?",
                (reservation.updated_at, reservation.account_id),
            )
        return ReservationCommit(stored_reservation, balances, transaction, False)

    def record_execution_update_disposition(
        self,
        *,
        account_id: str,
        source_sequence: int,
        stored_update: StoredOrderUpdate,
        command_id: str,
        disposition: StoredExecutionDisposition,
        processed_at: int,
    ) -> StoredExecutionEvent:
        if disposition is not StoredExecutionDisposition.NO_ACCOUNTING:
            raise ValueError("record-only update disposition must be no_accounting")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._insert_update_event(
                connection,
                account_id=account_id,
                source_sequence=source_sequence,
                stored_update=stored_update,
                command_id=command_id,
                disposition=disposition,
                processed_at=processed_at,
                transaction_id=None,
            )

    def commit_execution_fill(
        self,
        result: FillAccountingResult,
        *,
        policy: AccountPolicy,
        source_sequence: int,
        stored_update: StoredOrderUpdate,
        processed_at: int,
    ) -> FillCommit:
        reservation = result.reservation
        transaction = result.transaction
        if transaction.account_id != reservation.account_id:
            raise ValueError("fill transaction belongs to another account")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_event = connection.execute(
                """SELECT * FROM account_execution_update_events
                WHERE account_id=? AND (source_sequence=? OR update_id=?)""",
                (
                    reservation.account_id,
                    source_sequence,
                    stored_update.update.update_id,
                ),
            ).fetchone()
            if existing_event is not None:
                event = self._insert_update_event(
                    connection,
                    account_id=reservation.account_id,
                    source_sequence=source_sequence,
                    stored_update=stored_update,
                    command_id=reservation.command_id,
                    disposition=StoredExecutionDisposition.ACCOUNTED,
                    processed_at=processed_at,
                    transaction_id=transaction.transaction_id,
                )
                journal = self._load_journal_by_id(connection, transaction.transaction_id)
                if journal is None:
                    raise AccountStoreConflict("update event exists without its journal")
                current_reservation = self._stored_reservation(
                    connection,
                    reservation.command_id,
                )
                position = self._stored_position(
                    connection,
                    reservation.account_id,
                    reservation.symbol,
                )
                closed_position = None
                closed_row = connection.execute(
                    """SELECT * FROM account_closed_positions
                    WHERE close_update_id=?""",
                    (transaction.source_id,),
                ).fetchone()
                if closed_row is not None:
                    closed_position = self._decode_closed_position(
                        str(closed_row["payload"]),
                        int(closed_row["storage_sequence"]),
                    )
                cursor_row = connection.execute(
                    "SELECT payload FROM account_order_cursors WHERE order_id=?",
                    (result.cursor.order_id,),
                ).fetchone()
                if cursor_row is None:
                    raise AccountStoreConflict("update event exists without its order cursor")
                cursor = self._decode_cursor(str(cursor_row["payload"]))
                balances, _, _ = self._balances(connection, reservation.account_id)
                return FillCommit(
                    current_reservation,
                    position,
                    closed_position,
                    cursor,
                    balances,
                    journal,
                    event.duplicate,
                )

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
            if self._load_journal_by_id(connection, transaction.transaction_id) is not None:
                raise AccountStoreConflict("fill journal exists without its update event")

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
                if result.closed_position is None:
                    raise AccountStoreConflict("full close is missing its closed-position fact")
                if result.closed_position.position_id != current_position.position_id:
                    raise AccountStoreConflict("closed-position fact belongs to another position")
                if result.closed_position.close_update_id != transaction.source_id:
                    raise AccountStoreConflict("closed-position fact has another close update")
            elif result.closed_position is not None:
                raise AccountStoreConflict(
                    "active position fill cannot include a closed-position fact"
                )
            elif current_position is None:
                if result.position.version != 1:
                    raise AccountStoreConflict("new position must begin at version one")
            elif current_position.version + 1 != result.position.version:
                raise AccountStoreConflict("stale or skipped position version")

            journal, balances, duplicate = self._commit_transaction(connection, transaction)
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
            stored_closed_position = None
            if result.position is None:
                connection.execute(
                    "DELETE FROM account_positions WHERE account_id=? AND symbol=?",
                    (reservation.account_id, reservation.symbol),
                )
                assert result.closed_position is not None
                closed_cursor = connection.execute(
                    """INSERT INTO account_closed_positions(
                        closed_position_id,position_id,account_id,symbol,
                        close_update_id,closed_at,payload
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        result.closed_position.closed_position_id,
                        result.closed_position.position_id,
                        result.closed_position.account_id,
                        result.closed_position.symbol,
                        result.closed_position.close_update_id,
                        result.closed_position.closed_at,
                        self._encode_closed_position(result.closed_position),
                    ),
                )
                stored_closed_position = result.closed_position.with_storage_sequence(
                    int(closed_cursor.lastrowid)
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
            self._insert_update_event(
                connection,
                account_id=reservation.account_id,
                source_sequence=source_sequence,
                stored_update=stored_update,
                command_id=reservation.command_id,
                disposition=StoredExecutionDisposition.ACCOUNTED,
                processed_at=processed_at,
                transaction_id=transaction.transaction_id,
            )
            connection.execute(
                "UPDATE account_instances SET updated_at=max(updated_at,?) WHERE account_id=?",
                (transaction.event_time, reservation.account_id),
            )
        return FillCommit(
            reservation,
            result.position,
            stored_closed_position,
            result.cursor,
            balances,
            journal,
            False,
        )

    def latest_execution_command_sequence(self, account_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT coalesce(max(source_sequence),0)
                FROM account_execution_command_events WHERE account_id=?""",
                (account_id,),
            ).fetchone()
        return int(row[0])

    def latest_execution_update_sequence(self, account_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT coalesce(max(source_sequence),0)
                FROM account_execution_update_events WHERE account_id=?""",
                (account_id,),
            ).fetchone()
        return int(row[0])

    def latest_market_sequence(self, account_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT coalesce(max(source_sequence),0)
                FROM account_mark_history WHERE account_id=?""",
                (account_id,),
            ).fetchone()
        return int(row[0])

    def latest_funding_sequence(self, account_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT coalesce(max(rowid),0)
                FROM account_funding_applications WHERE account_id=?""",
                (account_id,),
            ).fetchone()
        return int(row[0])

    def consecutive_losses(self, account_id: str) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT payload FROM account_closed_positions
                WHERE account_id=? ORDER BY storage_sequence DESC""",
                (account_id,),
            ).fetchall()
        losses = 0
        for row in rows:
            closed = ClosedPosition.from_dict(json.loads(str(row["payload"])))
            if closed.net_pnl < 0:
                losses += 1
            else:
                break
        return losses
''',
)
append_once(
    EXECUTION_STORE,
    "    def command_id_for_order(",
    '''    def command_id_for_order(self, order_id: str) -> str:
        if not order_id:
            raise ValueError("order_id must not be empty")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT command_id FROM execution_orders WHERE order_id=?",
                (order_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"execution order not found: {order_id}")
        return str(row["command_id"])
''',
)

replace_once(
    ENGINE,
    '''from .domain import MarketRules
''',
    '''''',
)
replace_once(
    ENGINE,
    '''    CommandReservation,
    PositionSide,
    ReservationState,
    as_decimal,
''',
    '''    CommandReservation,
    as_decimal,
''',
)
replace_once(
    ENGINE,
    '''        if exact.delivery_state is not stored.delivery_state:
            raise PortfolioSourceError("execution command delivery state changed in transit")
''',
    '''''',
)
replace_once(
    ENGINE,
    '''            command_event = self.store.load_execution_command_event_by_id(
                self.settings.account_id,
                exact.update.order_id,
            )
            command_id = self.execution_store.command_id_for_order(
''',
    '''            command_id = self.execution_store.command_id_for_order(
''',
)
replace_once(
    ENGINE,
    '''                notional_after = (
                    (position.notional if position is not None else ZERO)
                    + cumulative_quote
                    - cursor.cumulative_quote_value
                )
''',
    '''                delta_quantity = (
                    cumulative_quantity - cursor.cumulative_filled_quantity
                )
                delta_quote = cumulative_quote - cursor.cumulative_quote_value
                if delta_quantity <= ZERO or delta_quote <= ZERO:
                    raise PortfolioAccountingError(
                        "positive open accounting requires fill quantity and quote"
                    )
                execution_price = delta_quote / delta_quantity
                projected_quantity = (
                    (position.quantity if position is not None else ZERO)
                    + delta_quantity
                )
                notional_after = projected_quantity * execution_price
''',
)
replace_once(
    ENGINE,
    '''        positions = tuple(self.store.list_positions(self.settings.account_id))
        halt_reasons = []
''',
    '''        positions = tuple(self.store.list_positions(self.settings.account_id))
        halt_reasons = []
        if instance.status is AccountStatus.HALTED:
            halt_reasons.append(AccountHaltReason.RECONCILIATION_FAILURE)
''',
)
