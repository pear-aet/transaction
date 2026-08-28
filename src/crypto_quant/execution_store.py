from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .execution import apply_order_update
from .execution_contracts import (
    ExecutionCommand,
    ExecutionCommandStatus,
    ExecutionMode,
    ExecutionOrder,
    OrderState,
    OrderUpdate,
)


class ExecutionCommandConflict(RuntimeError):
    """Raised when one risk decision resolves to different execution semantics."""


class ExecutionInstanceError(RuntimeError):
    """Raised when an execution instance changes its pinned identity."""


class ExecutionUpdateConflict(RuntimeError):
    """Raised when one update identifier is reused with different semantics."""


class ExecutionDeliveryState(StrEnum):
    RECORDED = "recorded"
    PENDING = "pending"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ExecutionInstance:
    engine_instance_id: str
    account_id: str
    policy_id: str
    policy_version: str
    policy_hash: str
    mode: ExecutionMode
    updated_at: int


@dataclass(frozen=True, slots=True)
class StoredExecutionCommand:
    command: ExecutionCommand
    delivery_state: ExecutionDeliveryState
    orders: tuple[ExecutionOrder, ...]
    dispatched_at: int | None
    completed_at: int | None
    expired_at: int | None
    failed_at: int | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class ExecutionCommit:
    instance: ExecutionInstance
    duplicate: bool
    command_inserted: bool
    stored_command: StoredExecutionCommand


@dataclass(frozen=True, slots=True)
class OrderUpdateCommit:
    order: ExecutionOrder
    duplicate: bool
    update_storage_sequence: int
    prior_state: OrderState


@dataclass(frozen=True, slots=True)
class StoredOrderUpdate:
    update: OrderUpdate
    storage_sequence: int
    prior_state: OrderState
    new_state: OrderState


class ExecutionStore:
    """Atomic execution plans, current orders and an append-only update journal."""

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
                CREATE TABLE IF NOT EXISTS execution_instances (
                    engine_instance_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('shadow','simulation')),
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0)
                );

                CREATE TABLE IF NOT EXISTS execution_commands (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL UNIQUE,
                    engine_instance_id TEXT NOT NULL,
                    source_decision_id TEXT NOT NULL,
                    source_decision_storage_sequence INTEGER NOT NULL
                        CHECK(source_decision_storage_sequence > 0),
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('shadow','simulation')),
                    payload TEXT NOT NULL,
                    delivery_state TEXT NOT NULL CHECK(
                        delivery_state IN (
                            'recorded','pending','dispatched','completed','expired','failed'
                        )
                    ),
                    dispatched_at INTEGER,
                    completed_at INTEGER,
                    expired_at INTEGER,
                    failed_at INTEGER,
                    failure_reason TEXT,
                    UNIQUE(engine_instance_id, source_decision_id),
                    UNIQUE(engine_instance_id, source_decision_storage_sequence),
                    FOREIGN KEY(engine_instance_id)
                        REFERENCES execution_instances(engine_instance_id)
                );
                CREATE INDEX IF NOT EXISTS execution_commands_outbox
                    ON execution_commands(delivery_state, storage_sequence);
                CREATE INDEX IF NOT EXISTS execution_commands_account_sequence
                    ON execution_commands(account_id, storage_sequence);

                CREATE TABLE IF NOT EXISTS execution_orders (
                    order_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    leg_type TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(
                        state IN (
                            'planned','dispatched','acknowledged','partially_filled',
                            'filled','cancel_pending','canceled','rejected','expired','failed'
                        )
                    ),
                    broker_order_id TEXT,
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
                    payload TEXT NOT NULL,
                    FOREIGN KEY(command_id)
                        REFERENCES execution_commands(command_id)
                );
                CREATE INDEX IF NOT EXISTS execution_orders_command
                    ON execution_orders(command_id, leg_type, order_id);
                CREATE INDEX IF NOT EXISTS execution_orders_state
                    ON execution_orders(state, updated_at);

                CREATE TABLE IF NOT EXISTS execution_order_updates (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id TEXT NOT NULL UNIQUE,
                    command_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    event_time INTEGER NOT NULL CHECK(event_time >= 0),
                    prior_state TEXT NOT NULL,
                    new_state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(command_id)
                        REFERENCES execution_commands(command_id),
                    FOREIGN KEY(order_id)
                        REFERENCES execution_orders(order_id)
                );
                CREATE INDEX IF NOT EXISTS execution_updates_order_sequence
                    ON execution_order_updates(order_id, storage_sequence);
                """
            )

    @staticmethod
    def _instance_from_row(row: sqlite3.Row) -> ExecutionInstance:
        return ExecutionInstance(
            str(row["engine_instance_id"]),
            str(row["account_id"]),
            str(row["policy_id"]),
            str(row["policy_version"]),
            str(row["policy_hash"]),
            ExecutionMode(row["mode"]),
            int(row["updated_at"]),
        )

    @staticmethod
    def _encode_command(command: ExecutionCommand) -> str:
        payload = command.to_dict()
        payload["sequence"] = 0
        payload["storage_sequence"] = None
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _decode_command(payload: str, storage_sequence: int) -> ExecutionCommand:
        command = ExecutionCommand.from_dict(json.loads(payload))
        return command.with_storage_sequence(storage_sequence)

    @staticmethod
    def _encode_order(order: ExecutionOrder) -> str:
        payload = order.to_dict()
        payload["sequence"] = 0
        payload["storage_sequence"] = None
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _decode_order(payload: str) -> ExecutionOrder:
        return ExecutionOrder.from_dict(json.loads(payload))

    @staticmethod
    def _encode_update(update: OrderUpdate) -> str:
        return json.dumps(
            update.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _decode_update(payload: str) -> OrderUpdate:
        value = json.loads(payload)
        return OrderUpdate(
            update_id=str(value["update_id"]),
            order_id=str(value["order_id"]),
            event_time=int(value["event_time"]),
            new_state=OrderState(value["new_state"]),
            cumulative_filled_quantity=float(value["cumulative_filled_quantity"]),
            cumulative_quote_value=float(value["cumulative_quote_value"]),
            cumulative_fee=float(value["cumulative_fee"]),
            average_fill_price=(
                None
                if value.get("average_fill_price") is None
                else float(value["average_fill_price"])
            ),
            broker_order_id=value.get("broker_order_id"),
            evidence=dict(value.get("evidence") or {}),
        )

    @staticmethod
    def _expected_delivery_state(command: ExecutionCommand) -> ExecutionDeliveryState:
        mapping = {
            ExecutionCommandStatus.SHADOW_RECORDED: ExecutionDeliveryState.RECORDED,
            ExecutionCommandStatus.DISPATCH_PENDING: ExecutionDeliveryState.PENDING,
            ExecutionCommandStatus.EXPIRED: ExecutionDeliveryState.EXPIRED,
            ExecutionCommandStatus.FAILED: ExecutionDeliveryState.FAILED,
        }
        try:
            return mapping[command.status]
        except KeyError as exc:
            raise ValueError("only initial execution command states may be committed") from exc

    @staticmethod
    def _same_command_semantics(
        left: ExecutionCommand,
        right: ExecutionCommand,
    ) -> bool:
        return left.semantic_dict() == right.semantic_dict()

    @staticmethod
    def _assert_instance(instance: ExecutionInstance, command: ExecutionCommand) -> None:
        expected = (
            command.account_id,
            command.execution_policy_id,
            command.execution_policy_version,
            command.execution_policy_hash,
            command.mode,
        )
        actual = (
            instance.account_id,
            instance.policy_id,
            instance.policy_version,
            instance.policy_hash,
            instance.mode,
        )
        if actual != expected:
            raise ExecutionInstanceError(
                "execution account, policy or mode changed; use a new engine_instance_id"
            )

    def _orders_for_command(
        self,
        connection: sqlite3.Connection,
        command_id: str,
    ) -> tuple[ExecutionOrder, ...]:
        rows = connection.execute(
            """SELECT payload FROM execution_orders
            WHERE command_id=? ORDER BY leg_type,order_id""",
            (command_id,),
        ).fetchall()
        return tuple(self._decode_order(str(row["payload"])) for row in rows)

    def _stored_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> StoredExecutionCommand:
        command = self._decode_command(str(row["payload"]), int(row["storage_sequence"]))
        return StoredExecutionCommand(
            command=command,
            delivery_state=ExecutionDeliveryState(row["delivery_state"]),
            orders=self._orders_for_command(connection, command.command_id),
            dispatched_at=(
                None if row["dispatched_at"] is None else int(row["dispatched_at"])
            ),
            completed_at=(
                None if row["completed_at"] is None else int(row["completed_at"])
            ),
            expired_at=None if row["expired_at"] is None else int(row["expired_at"]),
            failed_at=None if row["failed_at"] is None else int(row["failed_at"]),
            failure_reason=(
                None if row["failure_reason"] is None else str(row["failure_reason"])
            ),
        )

    def _load_stored(
        self,
        connection: sqlite3.Connection,
        command_id: str,
    ) -> StoredExecutionCommand | None:
        row = connection.execute(
            "SELECT * FROM execution_commands WHERE command_id=?",
            (command_id,),
        ).fetchone()
        return None if row is None else self._stored_from_row(connection, row)

    def load_instance(self, engine_instance_id: str) -> ExecutionInstance | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_instances WHERE engine_instance_id=?",
                (engine_instance_id,),
            ).fetchone()
        return None if row is None else self._instance_from_row(row)

    def load_command(self, command_id: str) -> StoredExecutionCommand | None:
        with self._connect() as connection:
            return self._load_stored(connection, command_id)

    def load_by_source_decision(
        self,
        engine_instance_id: str,
        source_decision_id: str,
    ) -> StoredExecutionCommand | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM execution_commands
                WHERE engine_instance_id=? AND source_decision_id=?""",
                (engine_instance_id, source_decision_id),
            ).fetchone()
            return None if row is None else self._stored_from_row(connection, row)

    def commit_command(
        self,
        command: ExecutionCommand,
        *,
        updated_at: int,
    ) -> ExecutionCommit:
        if updated_at < command.created_at:
            raise ValueError("execution persistence cannot precede command creation")
        if command.sequence != 0 or command.storage_sequence is not None:
            raise ValueError("only unsequenced, unstored execution commands may be committed")
        expected_state = self._expected_delivery_state(command)

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            instance_row = connection.execute(
                "SELECT * FROM execution_instances WHERE engine_instance_id=?",
                (command.engine_instance_id,),
            ).fetchone()
            if instance_row is None:
                connection.execute(
                    """INSERT INTO execution_instances(
                        engine_instance_id,account_id,policy_id,policy_version,
                        policy_hash,mode,updated_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        command.engine_instance_id,
                        command.account_id,
                        command.execution_policy_id,
                        command.execution_policy_version,
                        command.execution_policy_hash,
                        command.mode.value,
                        updated_at,
                    ),
                )
                instance = ExecutionInstance(
                    command.engine_instance_id,
                    command.account_id,
                    command.execution_policy_id,
                    command.execution_policy_version,
                    command.execution_policy_hash,
                    command.mode,
                    updated_at,
                )
            else:
                instance = self._instance_from_row(instance_row)
                self._assert_instance(instance, command)
                if updated_at < instance.updated_at:
                    raise ExecutionInstanceError("execution instance time cannot move backwards")
                connection.execute(
                    """UPDATE execution_instances SET updated_at=?
                    WHERE engine_instance_id=?""",
                    (updated_at, command.engine_instance_id),
                )
                instance = ExecutionInstance(
                    instance.engine_instance_id,
                    instance.account_id,
                    instance.policy_id,
                    instance.policy_version,
                    instance.policy_hash,
                    instance.mode,
                    updated_at,
                )

            existing_row = connection.execute(
                """SELECT * FROM execution_commands
                WHERE engine_instance_id=? AND source_decision_id=?""",
                (command.engine_instance_id, command.source_decision_id),
            ).fetchone()
            if existing_row is not None:
                stored = self._stored_from_row(connection, existing_row)
                if stored.command.command_id != command.command_id:
                    raise ExecutionCommandConflict(
                        f"execution command changed for risk decision {command.source_decision_id}"
                    )
                if not self._same_command_semantics(stored.command, command):
                    raise ExecutionCommandConflict(
                        f"execution command semantics changed: {command.command_id}"
                    )
                return ExecutionCommit(instance, True, False, stored)

            encoded = self._encode_command(command)
            try:
                connection.execute(
                    """INSERT INTO execution_commands(
                        command_id,engine_instance_id,source_decision_id,
                        source_decision_storage_sequence,account_id,symbol,mode,payload,
                        delivery_state,dispatched_at,completed_at,expired_at,failed_at,
                        failure_reason
                    ) VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL)""",
                    (
                        command.command_id,
                        command.engine_instance_id,
                        command.source_decision_id,
                        command.source_decision_storage_sequence,
                        command.account_id,
                        command.symbol,
                        command.mode.value,
                        encoded,
                        expected_state.value,
                    ),
                )
                for order in command.orders:
                    connection.execute(
                        """INSERT INTO execution_orders(
                            order_id,command_id,client_order_id,leg_type,state,
                            broker_order_id,updated_at,payload
                        ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            order.order_id,
                            command.command_id,
                            order.client_order_id,
                            order.leg_type.value,
                            order.state.value,
                            order.broker_order_id,
                            order.updated_at,
                            self._encode_order(order),
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ExecutionCommandConflict(
                    f"execution identity collision for command {command.command_id}"
                ) from exc

            stored = self._load_stored(connection, command.command_id)
            if stored is None:
                raise RuntimeError("execution command insert completed without a readable row")
        return ExecutionCommit(instance, False, True, stored)

    def pending_commands(
        self,
        *,
        engine_instance_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[StoredExecutionCommand]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("command limit must be in [1, 10000]")
        query = """SELECT * FROM execution_commands
        WHERE delivery_state='pending'
        AND storage_sequence>?
        AND (? IS NULL OR engine_instance_id=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (after_sequence, engine_instance_id, engine_instance_id, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._stored_from_row(connection, row) for row in rows]

    def _transition_delivery(
        self,
        command_id: str,
        *,
        expected: frozenset[ExecutionDeliveryState],
        target: ExecutionDeliveryState,
        timestamp: int,
        reason: str | None = None,
    ) -> bool:
        if not command_id or timestamp < 0:
            raise ValueError("invalid execution delivery transition")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"execution command not found: {command_id}")
            stored = self._stored_from_row(connection, row)
            state = stored.delivery_state
            if state is target:
                if target is ExecutionDeliveryState.FAILED:
                    if stored.failure_reason != reason:
                        raise ExecutionCommandConflict(
                            f"execution failure reason changed: {command_id}"
                        )
                return False
            if state not in expected:
                raise ValueError(
                    f"execution delivery cannot move from {state.value} to {target.value}"
                )
            if target is ExecutionDeliveryState.DISPATCHED:
                if timestamp > stored.command.valid_until:
                    raise ValueError("expired execution command cannot be dispatched")
            if target is ExecutionDeliveryState.COMPLETED:
                if stored.dispatched_at is None or timestamp < stored.dispatched_at:
                    raise ValueError("execution completion cannot precede dispatch")
            if target is ExecutionDeliveryState.EXPIRED:
                if timestamp <= stored.command.valid_until:
                    raise ValueError("execution command cannot expire before validity ends")
            if target is ExecutionDeliveryState.FAILED:
                if not reason:
                    raise ValueError("failed execution command requires a reason")
                if stored.dispatched_at is not None and timestamp < stored.dispatched_at:
                    raise ValueError("execution failure cannot precede dispatch")

            if target is ExecutionDeliveryState.DISPATCHED:
                query = """UPDATE execution_commands SET
                    delivery_state='dispatched',dispatched_at=? WHERE command_id=?"""
                parameters: tuple[object, ...] = (timestamp, command_id)
            elif target is ExecutionDeliveryState.COMPLETED:
                query = """UPDATE execution_commands SET
                    delivery_state='completed',completed_at=? WHERE command_id=?"""
                parameters = (timestamp, command_id)
            elif target is ExecutionDeliveryState.EXPIRED:
                query = """UPDATE execution_commands SET
                    delivery_state='expired',expired_at=? WHERE command_id=?"""
                parameters = (timestamp, command_id)
            else:
                query = """UPDATE execution_commands SET
                    delivery_state='failed',failed_at=?,failure_reason=? WHERE command_id=?"""
                parameters = (timestamp, reason, command_id)
            connection.execute(query, parameters)
        return True

    def mark_dispatched(self, command_id: str, dispatched_at: int) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset({ExecutionDeliveryState.PENDING}),
            target=ExecutionDeliveryState.DISPATCHED,
            timestamp=dispatched_at,
        )

    def mark_completed(self, command_id: str, completed_at: int) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset({ExecutionDeliveryState.DISPATCHED}),
            target=ExecutionDeliveryState.COMPLETED,
            timestamp=completed_at,
        )

    def mark_expired(self, command_id: str, expired_at: int) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset({ExecutionDeliveryState.PENDING}),
            target=ExecutionDeliveryState.EXPIRED,
            timestamp=expired_at,
        )

    def mark_failed(self, command_id: str, failed_at: int, reason: str) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset(
                {
                    ExecutionDeliveryState.PENDING,
                    ExecutionDeliveryState.DISPATCHED,
                }
            ),
            target=ExecutionDeliveryState.FAILED,
            timestamp=failed_at,
            reason=reason,
        )

    def load_order(self, order_id: str) -> ExecutionOrder | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM execution_orders WHERE order_id=?",
                (order_id,),
            ).fetchone()
        return None if row is None else self._decode_order(str(row["payload"]))

    def apply_order_update(self, update: OrderUpdate) -> OrderUpdateCommit:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate_row = connection.execute(
                "SELECT * FROM execution_order_updates WHERE update_id=?",
                (update.update_id,),
            ).fetchone()
            if duplicate_row is not None:
                existing = self._decode_update(str(duplicate_row["payload"]))
                if existing.to_dict() != update.to_dict():
                    raise ExecutionUpdateConflict(
                        f"order update identifier collision: {update.update_id}"
                    )
                order_row = connection.execute(
                    "SELECT payload FROM execution_orders WHERE order_id=?",
                    (update.order_id,),
                ).fetchone()
                if order_row is None:
                    raise RuntimeError("order update exists without its current order")
                return OrderUpdateCommit(
                    self._decode_order(str(order_row["payload"])),
                    True,
                    int(duplicate_row["storage_sequence"]),
                    OrderState(duplicate_row["prior_state"]),
                )

            order_row = connection.execute(
                "SELECT * FROM execution_orders WHERE order_id=?",
                (update.order_id,),
            ).fetchone()
            if order_row is None:
                raise KeyError(f"execution order not found: {update.order_id}")
            current = self._decode_order(str(order_row["payload"]))
            updated = apply_order_update(current, update)
            cursor = connection.execute(
                """INSERT INTO execution_order_updates(
                    update_id,command_id,order_id,event_time,prior_state,new_state,payload
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    update.update_id,
                    current.command_id,
                    current.order_id,
                    update.event_time,
                    current.state.value,
                    updated.state.value,
                    self._encode_update(update),
                ),
            )
            connection.execute(
                """UPDATE execution_orders SET
                    state=?,broker_order_id=?,updated_at=?,payload=?
                WHERE order_id=?""",
                (
                    updated.state.value,
                    updated.broker_order_id,
                    updated.updated_at,
                    self._encode_order(updated),
                    updated.order_id,
                ),
            )
            storage_sequence = int(cursor.lastrowid)
        return OrderUpdateCommit(updated, False, storage_sequence, current.state)

    def order_updates(self, order_id: str) -> list[StoredOrderUpdate]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM execution_order_updates
                WHERE order_id=? ORDER BY storage_sequence""",
                (order_id,),
            ).fetchall()
        return [
            StoredOrderUpdate(
                update=self._decode_update(str(row["payload"])),
                storage_sequence=int(row["storage_sequence"]),
                prior_state=OrderState(row["prior_state"]),
                new_state=OrderState(row["new_state"]),
            )
            for row in rows
        ]

    def latest_command_sequence(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT coalesce(max(storage_sequence),0) FROM execution_commands"
            ).fetchone()
        return int(row[0])

    def oldest_pending_created_at(self) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT payload FROM execution_commands
                WHERE delivery_state='pending' ORDER BY storage_sequence LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        return ExecutionCommand.from_dict(json.loads(str(row["payload"]))).created_at

    def metrics(self) -> dict[str, int]:
        with self._connect() as connection:
            instances = int(
                connection.execute("SELECT count(*) FROM execution_instances").fetchone()[0]
            )
            command_rows = connection.execute(
                """SELECT delivery_state,count(*) FROM execution_commands
                GROUP BY delivery_state"""
            ).fetchall()
            command_counts = {str(row[0]): int(row[1]) for row in command_rows}
            order_rows = connection.execute(
                "SELECT state,count(*) FROM execution_orders GROUP BY state"
            ).fetchall()
            order_counts = {str(row[0]): int(row[1]) for row in order_rows}
            updates = int(
                connection.execute("SELECT count(*) FROM execution_order_updates").fetchone()[0]
            )
            latest = int(
                connection.execute(
                    "SELECT coalesce(max(storage_sequence),0) FROM execution_commands"
                ).fetchone()[0]
            )
        return {
            "instances": instances,
            "commands": sum(command_counts.values()),
            "recorded_commands": command_counts.get(ExecutionDeliveryState.RECORDED.value, 0),
            "pending_commands": command_counts.get(ExecutionDeliveryState.PENDING.value, 0),
            "dispatched_commands": command_counts.get(
                ExecutionDeliveryState.DISPATCHED.value, 0
            ),
            "completed_commands": command_counts.get(ExecutionDeliveryState.COMPLETED.value, 0),
            "expired_commands": command_counts.get(ExecutionDeliveryState.EXPIRED.value, 0),
            "failed_commands": command_counts.get(ExecutionDeliveryState.FAILED.value, 0),
            "orders": sum(order_counts.values()),
            "planned_orders": order_counts.get(OrderState.PLANNED.value, 0),
            "partially_filled_orders": order_counts.get(
                OrderState.PARTIALLY_FILLED.value, 0
            ),
            "filled_orders": order_counts.get(OrderState.FILLED.value, 0),
            "failed_orders": order_counts.get(OrderState.FAILED.value, 0),
            "order_updates": updates,
            "latest_command_sequence": latest,
        }
