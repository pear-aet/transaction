from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .strategy_contracts import StrategyIntent


class StrategyIntentConflict(RuntimeError):
    """Raised when one deterministic intent ID resolves to different semantics."""


class StrategyCheckpointError(RuntimeError):
    """Raised when a strategy checkpoint would move backwards or change history."""


@dataclass(frozen=True, slots=True)
class StrategyCheckpoint:
    instance_id: str
    symbol: str
    last_event_time: int
    last_event_id: str
    updated_at: int


@dataclass(frozen=True, slots=True)
class EvaluationCommit:
    checkpoint: StrategyCheckpoint
    event_duplicate: bool
    intent_inserted: bool


@dataclass(frozen=True, slots=True)
class StoredIntent:
    intent: StrategyIntent
    published_at: int | None


class StrategyStore:
    """Atomic strategy checkpoints plus a persistent at-least-once intent outbox."""

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
                CREATE TABLE IF NOT EXISTS strategy_checkpoints (
                    instance_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    last_event_time INTEGER NOT NULL CHECK(last_event_time >= 0),
                    last_event_id TEXT NOT NULL,
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
                    PRIMARY KEY(instance_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS strategy_intents (
                    intent_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    decision_time INTEGER NOT NULL CHECK(decision_time >= 0),
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    published_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS strategy_intents_outbox
                    ON strategy_intents(published_at, decision_time, intent_id);
                CREATE INDEX IF NOT EXISTS strategy_intents_instance_time
                    ON strategy_intents(instance_id, symbol, decision_time, intent_id);
                """
            )

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> StrategyCheckpoint:
        return StrategyCheckpoint(
            str(row["instance_id"]),
            str(row["symbol"]),
            int(row["last_event_time"]),
            str(row["last_event_id"]),
            int(row["updated_at"]),
        )

    @staticmethod
    def _encode_intent(intent: StrategyIntent) -> str:
        return json.dumps(
            intent.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _decode_intent(payload: str) -> StrategyIntent:
        return StrategyIntent.from_dict(json.loads(payload))

    @staticmethod
    def _same_semantics(left: StrategyIntent, right: StrategyIntent) -> bool:
        return left.semantic_dict() == right.semantic_dict()

    def load_checkpoint(self, instance_id: str, symbol: str) -> StrategyCheckpoint | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM strategy_checkpoints
                WHERE instance_id=? AND symbol=?""",
                (instance_id, symbol),
            ).fetchone()
        return None if row is None else self._checkpoint_from_row(row)

    def commit_evaluation(
        self,
        *,
        instance_id: str,
        symbol: str,
        event_time: int,
        event_id: str,
        updated_at: int,
        intent: StrategyIntent | None,
    ) -> EvaluationCommit:
        if not instance_id or not symbol or not event_id:
            raise ValueError("checkpoint identifiers must not be empty")
        if event_time < 0 or updated_at < 0:
            raise ValueError("checkpoint timestamps must be non-negative")
        if intent is not None:
            if intent.instance_id != instance_id or intent.symbol != symbol:
                raise ValueError("intent does not match the checkpoint partition")
            if intent.decision_time != event_time or intent.source_event_id != event_id:
                raise ValueError("intent does not match the evaluated market event")

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM strategy_checkpoints
                WHERE instance_id=? AND symbol=?""",
                (instance_id, symbol),
            ).fetchone()
            if row is not None:
                current = self._checkpoint_from_row(row)
                if event_time < current.last_event_time:
                    raise StrategyCheckpointError(
                        f"checkpoint cannot move backwards for {instance_id} {symbol}"
                    )
                if event_time == current.last_event_time:
                    if event_id != current.last_event_id:
                        raise StrategyCheckpointError(
                            f"checkpoint event changed at {event_time} for {instance_id} {symbol}"
                        )
                    return EvaluationCommit(current, True, False)

            intent_inserted = False
            if intent is not None:
                encoded = self._encode_intent(intent)
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO strategy_intents(
                        intent_id,instance_id,strategy_id,strategy_version,symbol,
                        decision_time,action,payload,published_at
                    ) VALUES(?,?,?,?,?,?,?,?,NULL)""",
                    (
                        intent.intent_id,
                        intent.instance_id,
                        intent.strategy_id,
                        intent.strategy_version,
                        intent.symbol,
                        intent.decision_time,
                        intent.action.value,
                        encoded,
                    ),
                )
                intent_inserted = cursor.rowcount == 1
                if not intent_inserted:
                    existing_row = connection.execute(
                        "SELECT payload FROM strategy_intents WHERE intent_id=?",
                        (intent.intent_id,),
                    ).fetchone()
                    if existing_row is None:
                        raise RuntimeError("intent conflict lookup returned no row")
                    existing = self._decode_intent(str(existing_row["payload"]))
                    if not self._same_semantics(existing, intent):
                        raise StrategyIntentConflict(
                            f"strategy intent ID collision: {intent.intent_id}"
                        )

            connection.execute(
                """INSERT INTO strategy_checkpoints(
                    instance_id,symbol,last_event_time,last_event_id,updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(instance_id,symbol) DO UPDATE SET
                    last_event_time=excluded.last_event_time,
                    last_event_id=excluded.last_event_id,
                    updated_at=excluded.updated_at""",
                (instance_id, symbol, event_time, event_id, updated_at),
            )
            checkpoint = StrategyCheckpoint(
                instance_id,
                symbol,
                event_time,
                event_id,
                updated_at,
            )
        return EvaluationCommit(checkpoint, False, intent_inserted)

    def pending_intents(
        self,
        *,
        instance_id: str | None = None,
        limit: int = 1000,
    ) -> list[StrategyIntent]:
        if not 1 <= limit <= 10_000:
            raise ValueError("intent limit must be in [1, 10000]")
        query = "SELECT payload FROM strategy_intents WHERE published_at IS NULL"
        parameters: list[Any] = []
        if instance_id is not None:
            query += " AND instance_id=?"
            parameters.append(instance_id)
        query += " ORDER BY decision_time,intent_id LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [self._decode_intent(str(row["payload"])) for row in rows]

    def mark_published(self, intent_id: str, published_at: int) -> bool:
        if not intent_id or published_at < 0:
            raise ValueError("invalid intent publication marker")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT published_at FROM strategy_intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"strategy intent not found: {intent_id}")
            if row["published_at"] is not None:
                return False
            connection.execute(
                "UPDATE strategy_intents SET published_at=? WHERE intent_id=?",
                (published_at, intent_id),
            )
        return True

    def list_intents(
        self,
        *,
        instance_id: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[StoredIntent]:
        if not 1 <= limit <= 10_000:
            raise ValueError("intent limit must be in [1, 10000]")
        clauses: list[str] = []
        parameters: list[Any] = []
        if instance_id is not None:
            clauses.append("instance_id=?")
            parameters.append(instance_id)
        if symbol is not None:
            clauses.append("symbol=?")
            parameters.append(symbol)
        query = "SELECT payload,published_at FROM strategy_intents"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY decision_time DESC,intent_id DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [
            StoredIntent(
                self._decode_intent(str(row["payload"])),
                None if row["published_at"] is None else int(row["published_at"]),
            )
            for row in rows
        ]

    def reset_checkpoint(self, instance_id: str, symbol: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """DELETE FROM strategy_checkpoints
                WHERE instance_id=? AND symbol=?""",
                (instance_id, symbol),
            )
        return cursor.rowcount == 1

    def metrics(self) -> dict[str, int]:
        with self._connect() as connection:
            checkpoints = int(
                connection.execute("SELECT count(*) FROM strategy_checkpoints").fetchone()[0]
            )
            intents = int(connection.execute("SELECT count(*) FROM strategy_intents").fetchone()[0])
            pending = int(
                connection.execute(
                    "SELECT count(*) FROM strategy_intents WHERE published_at IS NULL"
                ).fetchone()[0]
            )
        return {
            "checkpoints": checkpoints,
            "intents": intents,
            "pending_intents": pending,
        }
