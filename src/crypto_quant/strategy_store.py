from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .strategy_contracts import StrategyIntent


class StrategyIntentConflict(RuntimeError):
    """Raised when deterministic intent or decision semantics change."""


class StrategyCheckpointError(RuntimeError):
    """Raised when a checkpoint moves backwards or changes configuration."""


class StrategyEvaluationMode(StrEnum):
    LIVE = "live"
    INITIAL = "initial"
    REPLAY = "replay"
    BASELINE = "baseline"
    RESET = "reset"


class StrategyDeliveryState(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class StrategyCheckpoint:
    instance_id: str
    symbol: str
    strategy_id: str
    strategy_version: str
    parameter_hash: str
    high_timeframe: str
    low_timeframe: str
    last_event_time: int
    last_event_id: str
    updated_at: int


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    instance_id: str
    symbol: str
    event_time: int
    event_id: str
    strategy_id: str
    strategy_version: str
    parameter_hash: str
    mode: StrategyEvaluationMode
    intent_id: str | None
    evaluated_at: int


@dataclass(frozen=True, slots=True)
class StoredIntent:
    intent: StrategyIntent
    delivery_state: StrategyDeliveryState
    published_at: int | None
    suppression_reason: str | None


@dataclass(frozen=True, slots=True)
class EvaluationCommit:
    checkpoint: StrategyCheckpoint
    event_duplicate: bool
    intent_inserted: bool
    stored_intent: StoredIntent | None


class StrategyStore:
    """Atomic decisions, version-pinned checkpoints and an intent replay log."""

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
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    parameter_hash TEXT NOT NULL,
                    high_timeframe TEXT NOT NULL,
                    low_timeframe TEXT NOT NULL,
                    last_event_time INTEGER NOT NULL CHECK(last_event_time >= 0),
                    last_event_id TEXT NOT NULL,
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
                    PRIMARY KEY(instance_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS strategy_intents (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL UNIQUE,
                    instance_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    decision_time INTEGER NOT NULL CHECK(decision_time >= 0),
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    delivery_state TEXT NOT NULL CHECK(
                        delivery_state IN ('pending','published','suppressed')
                    ),
                    published_at INTEGER,
                    suppression_reason TEXT,
                    UNIQUE(instance_id, symbol, decision_time)
                );
                CREATE INDEX IF NOT EXISTS strategy_intents_outbox
                    ON strategy_intents(delivery_state, storage_sequence);
                CREATE INDEX IF NOT EXISTS strategy_intents_instance_sequence
                    ON strategy_intents(instance_id, symbol, storage_sequence);

                CREATE TABLE IF NOT EXISTS strategy_evaluations (
                    instance_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_time INTEGER NOT NULL CHECK(event_time >= 0),
                    event_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    parameter_hash TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(
                        mode IN ('live','initial','replay','baseline','reset')
                    ),
                    intent_id TEXT,
                    evaluated_at INTEGER NOT NULL CHECK(evaluated_at >= 0),
                    PRIMARY KEY(instance_id, symbol, event_time),
                    UNIQUE(instance_id, symbol, event_id),
                    FOREIGN KEY(intent_id) REFERENCES strategy_intents(intent_id)
                );
                CREATE INDEX IF NOT EXISTS strategy_evaluations_event_time
                    ON strategy_evaluations(instance_id, symbol, event_time);
                """
            )

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> StrategyCheckpoint:
        return StrategyCheckpoint(
            str(row["instance_id"]),
            str(row["symbol"]),
            str(row["strategy_id"]),
            str(row["strategy_version"]),
            str(row["parameter_hash"]),
            str(row["high_timeframe"]),
            str(row["low_timeframe"]),
            int(row["last_event_time"]),
            str(row["last_event_id"]),
            int(row["updated_at"]),
        )

    @staticmethod
    def _evaluation_from_row(row: sqlite3.Row) -> StrategyEvaluation:
        return StrategyEvaluation(
            str(row["instance_id"]),
            str(row["symbol"]),
            int(row["event_time"]),
            str(row["event_id"]),
            str(row["strategy_id"]),
            str(row["strategy_version"]),
            str(row["parameter_hash"]),
            StrategyEvaluationMode(row["mode"]),
            None if row["intent_id"] is None else str(row["intent_id"]),
            int(row["evaluated_at"]),
        )

    @staticmethod
    def _encode_intent(intent: StrategyIntent) -> str:
        payload = intent.to_dict()
        payload["storage_sequence"] = None
        payload["sequence"] = 0
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _decode_intent(payload: str, storage_sequence: int) -> StrategyIntent:
        return StrategyIntent.from_dict(json.loads(payload)).with_storage_sequence(storage_sequence)

    @classmethod
    def _stored_intent_from_row(cls, row: sqlite3.Row) -> StoredIntent:
        return StoredIntent(
            cls._decode_intent(str(row["payload"]), int(row["storage_sequence"])),
            StrategyDeliveryState(row["delivery_state"]),
            None if row["published_at"] is None else int(row["published_at"]),
            None if row["suppression_reason"] is None else str(row["suppression_reason"]),
        )

    @staticmethod
    def _same_semantics(left: StrategyIntent, right: StrategyIntent) -> bool:
        return left.semantic_dict() == right.semantic_dict()

    @staticmethod
    def _signature(
        strategy_id: str,
        strategy_version: str,
        parameter_hash: str,
        high_timeframe: str,
        low_timeframe: str,
    ) -> tuple[str, str, str, str, str]:
        return (
            strategy_id,
            strategy_version,
            parameter_hash,
            high_timeframe,
            low_timeframe,
        )

    @classmethod
    def _assert_checkpoint_signature(
        cls,
        checkpoint: StrategyCheckpoint,
        strategy_id: str,
        strategy_version: str,
        parameter_hash: str,
        high_timeframe: str,
        low_timeframe: str,
    ) -> None:
        expected = cls._signature(
            strategy_id,
            strategy_version,
            parameter_hash,
            high_timeframe,
            low_timeframe,
        )
        actual = cls._signature(
            checkpoint.strategy_id,
            checkpoint.strategy_version,
            checkpoint.parameter_hash,
            checkpoint.high_timeframe,
            checkpoint.low_timeframe,
        )
        if actual != expected:
            raise StrategyCheckpointError(
                "strategy configuration changed for an existing instance; use a new instance_id"
            )

    def load_checkpoint(self, instance_id: str, symbol: str) -> StrategyCheckpoint | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM strategy_checkpoints
                WHERE instance_id=? AND symbol=?""",
                (instance_id, symbol),
            ).fetchone()
        return None if row is None else self._checkpoint_from_row(row)

    def load_evaluation(
        self, instance_id: str, symbol: str, event_time: int
    ) -> StrategyEvaluation | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM strategy_evaluations
                WHERE instance_id=? AND symbol=? AND event_time=?""",
                (instance_id, symbol, event_time),
            ).fetchone()
        return None if row is None else self._evaluation_from_row(row)

    def _load_stored_intent(
        self, connection: sqlite3.Connection, intent_id: str
    ) -> StoredIntent | None:
        row = connection.execute(
            "SELECT * FROM strategy_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        return None if row is None else self._stored_intent_from_row(row)

    def _verify_duplicate_evaluation(
        self,
        connection: sqlite3.Connection,
        current: StrategyCheckpoint,
        *,
        event_time: int,
        event_id: str,
        mode: StrategyEvaluationMode,
        intent: StrategyIntent | None,
        delivery_required: bool,
    ) -> EvaluationCommit:
        row = connection.execute(
            """SELECT * FROM strategy_evaluations
            WHERE instance_id=? AND symbol=? AND event_time=?""",
            (current.instance_id, current.symbol, event_time),
        ).fetchone()
        if row is None:
            raise StrategyCheckpointError(
                "checkpoint exists without its immutable evaluation record"
            )
        evaluation = self._evaluation_from_row(row)
        if evaluation.event_id != event_id:
            raise StrategyCheckpointError(
                f"checkpoint event changed at {event_time} for "
                f"{current.instance_id} {current.symbol}"
            )
        if evaluation.mode is not mode:
            raise StrategyIntentConflict(
                f"evaluation mode changed at {event_time}: {evaluation.mode.value} != {mode.value}"
            )
        proposed_intent_id = None if intent is None else intent.intent_id
        if evaluation.intent_id != proposed_intent_id:
            raise StrategyIntentConflict(
                f"strategy decision changed at {event_time}: "
                f"{evaluation.intent_id} != {proposed_intent_id}"
            )
        stored: StoredIntent | None = None
        if intent is not None:
            stored = self._load_stored_intent(connection, intent.intent_id)
            if stored is None or not self._same_semantics(stored.intent, intent):
                raise StrategyIntentConflict(
                    f"strategy intent semantics changed: {intent.intent_id}"
                )
            expected_suppressed = not delivery_required
            if (stored.delivery_state is StrategyDeliveryState.SUPPRESSED) != expected_suppressed:
                raise StrategyIntentConflict(
                    f"strategy intent delivery disposition changed: {intent.intent_id}"
                )
        return EvaluationCommit(current, True, False, stored)

    def commit_evaluation(
        self,
        *,
        instance_id: str,
        symbol: str,
        event_time: int,
        event_id: str,
        updated_at: int,
        strategy_id: str,
        strategy_version: str,
        parameter_hash: str,
        high_timeframe: str,
        low_timeframe: str,
        intent: StrategyIntent | None,
        mode: StrategyEvaluationMode = StrategyEvaluationMode.LIVE,
        delivery_required: bool = True,
        suppression_reason: str | None = None,
    ) -> EvaluationCommit:
        text_values = (
            instance_id,
            symbol,
            event_id,
            strategy_id,
            strategy_version,
            parameter_hash,
            high_timeframe,
            low_timeframe,
        )
        if any(not value for value in text_values):
            raise ValueError("evaluation identifiers and strategy signature must not be empty")
        if event_time < 0 or updated_at < 0:
            raise ValueError("evaluation timestamps must be non-negative")
        if delivery_required and mode not in {
            StrategyEvaluationMode.LIVE,
            StrategyEvaluationMode.INITIAL,
        }:
            raise ValueError("historical evaluation modes cannot require live delivery")
        if intent is None:
            suppression_reason = None
        else:
            expected_signature = self._signature(
                strategy_id,
                strategy_version,
                parameter_hash,
                high_timeframe,
                low_timeframe,
            )
            actual_signature = self._signature(
                intent.strategy_id,
                intent.strategy_version,
                intent.parameter_hash,
                intent.high_timeframe,
                intent.low_timeframe,
            )
            if intent.instance_id != instance_id or intent.symbol != symbol:
                raise ValueError("intent does not match the evaluation partition")
            if intent.decision_time != event_time or intent.source_event_id != event_id:
                raise ValueError("intent does not match the evaluated market event")
            if actual_signature != expected_signature:
                raise ValueError("intent does not match the strategy signature")
            if not delivery_required and not suppression_reason:
                raise ValueError("suppressed intent requires a suppression_reason")
        if mode in {StrategyEvaluationMode.BASELINE, StrategyEvaluationMode.RESET} and intent:
            raise ValueError(f"{mode.value} evaluation cannot contain a strategy intent")

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM strategy_checkpoints
                WHERE instance_id=? AND symbol=?""",
                (instance_id, symbol),
            ).fetchone()
            current = None if row is None else self._checkpoint_from_row(row)
            if current is not None:
                self._assert_checkpoint_signature(
                    current,
                    strategy_id,
                    strategy_version,
                    parameter_hash,
                    high_timeframe,
                    low_timeframe,
                )
                if event_time < current.last_event_time:
                    raise StrategyCheckpointError(
                        f"checkpoint cannot move backwards for {instance_id} {symbol}"
                    )
                if event_time == current.last_event_time:
                    return self._verify_duplicate_evaluation(
                        connection,
                        current,
                        event_time=event_time,
                        event_id=event_id,
                        mode=mode,
                        intent=intent,
                        delivery_required=delivery_required,
                    )

            stored_intent: StoredIntent | None = None
            intent_inserted = False
            if intent is not None:
                encoded = self._encode_intent(intent)
                delivery_state = (
                    StrategyDeliveryState.PENDING
                    if delivery_required
                    else StrategyDeliveryState.SUPPRESSED
                )
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO strategy_intents(
                        intent_id,instance_id,strategy_id,strategy_version,symbol,
                        decision_time,action,payload,delivery_state,published_at,
                        suppression_reason
                    ) VALUES(?,?,?,?,?,?,?,?,?,NULL,?)""",
                    (
                        intent.intent_id,
                        intent.instance_id,
                        intent.strategy_id,
                        intent.strategy_version,
                        intent.symbol,
                        intent.decision_time,
                        intent.action.value,
                        encoded,
                        delivery_state.value,
                        suppression_reason,
                    ),
                )
                intent_inserted = cursor.rowcount == 1
                stored_intent = self._load_stored_intent(connection, intent.intent_id)
                if stored_intent is None:
                    competing_row = connection.execute(
                        """SELECT * FROM strategy_intents
                        WHERE instance_id=? AND symbol=? AND decision_time=?""",
                        (instance_id, symbol, event_time),
                    ).fetchone()
                    if competing_row is None:
                        raise RuntimeError("intent insert completed without a readable row")
                    competing = self._stored_intent_from_row(competing_row)
                    raise StrategyIntentConflict(
                        f"multiple strategy intents for one decision time: "
                        f"{competing.intent.intent_id} != {intent.intent_id}"
                    )
                if not self._same_semantics(stored_intent.intent, intent):
                    raise StrategyIntentConflict(
                        f"strategy intent ID collision: {intent.intent_id}"
                    )
                if stored_intent.delivery_state is not delivery_state:
                    raise StrategyIntentConflict(
                        f"strategy intent delivery disposition changed: {intent.intent_id}"
                    )

            connection.execute(
                """INSERT INTO strategy_evaluations(
                    instance_id,symbol,event_time,event_id,strategy_id,
                    strategy_version,parameter_hash,mode,intent_id,evaluated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    instance_id,
                    symbol,
                    event_time,
                    event_id,
                    strategy_id,
                    strategy_version,
                    parameter_hash,
                    mode.value,
                    None if intent is None else intent.intent_id,
                    updated_at,
                ),
            )
            connection.execute(
                """INSERT INTO strategy_checkpoints(
                    instance_id,symbol,strategy_id,strategy_version,parameter_hash,
                    high_timeframe,low_timeframe,last_event_time,last_event_id,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instance_id,symbol) DO UPDATE SET
                    strategy_id=excluded.strategy_id,
                    strategy_version=excluded.strategy_version,
                    parameter_hash=excluded.parameter_hash,
                    high_timeframe=excluded.high_timeframe,
                    low_timeframe=excluded.low_timeframe,
                    last_event_time=excluded.last_event_time,
                    last_event_id=excluded.last_event_id,
                    updated_at=excluded.updated_at""",
                (
                    instance_id,
                    symbol,
                    strategy_id,
                    strategy_version,
                    parameter_hash,
                    high_timeframe,
                    low_timeframe,
                    event_time,
                    event_id,
                    updated_at,
                ),
            )
            checkpoint = StrategyCheckpoint(
                instance_id,
                symbol,
                strategy_id,
                strategy_version,
                parameter_hash,
                high_timeframe,
                low_timeframe,
                event_time,
                event_id,
                updated_at,
            )
        return EvaluationCommit(checkpoint, False, intent_inserted, stored_intent)

    def pending_intents(
        self,
        *,
        instance_id: str | None = None,
        limit: int = 1000,
    ) -> list[StrategyIntent]:
        if not 1 <= limit <= 10_000:
            raise ValueError("intent limit must be in [1, 10000]")
        query = """SELECT * FROM strategy_intents
        WHERE delivery_state='pending'
        AND (? IS NULL OR instance_id=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (instance_id, instance_id, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._stored_intent_from_row(row).intent for row in rows]

    def mark_published(self, intent_id: str, published_at: int) -> bool:
        if not intent_id or published_at < 0:
            raise ValueError("invalid intent publication marker")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT delivery_state FROM strategy_intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"strategy intent not found: {intent_id}")
            state = StrategyDeliveryState(row["delivery_state"])
            if state is StrategyDeliveryState.PUBLISHED:
                return False
            if state is StrategyDeliveryState.SUPPRESSED:
                raise ValueError("suppressed strategy intent cannot be published")
            connection.execute(
                """UPDATE strategy_intents
                SET delivery_state='published',published_at=?
                WHERE intent_id=?""",
                (published_at, intent_id),
            )
        return True

    def load_intents(
        self,
        *,
        after_sequence: int = 0,
        through_sequence: int | None = None,
        instance_id: str | None = None,
        symbol: str | None = None,
        limit: int = 1000,
    ) -> list[StoredIntent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("intent limit must be in [1, 10000]")
        if through_sequence is not None and through_sequence < after_sequence:
            return []
        query = """SELECT * FROM strategy_intents
        WHERE storage_sequence>?
        AND (? IS NULL OR storage_sequence<=?)
        AND (? IS NULL OR instance_id=?)
        AND (? IS NULL OR symbol=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (
            after_sequence,
            through_sequence,
            through_sequence,
            instance_id,
            instance_id,
            symbol,
            symbol,
            limit,
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._stored_intent_from_row(row) for row in rows]

    def latest_intent_sequence(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT coalesce(max(storage_sequence),0) FROM strategy_intents"
            ).fetchone()
        return int(row[0])

    def list_intents(
        self,
        *,
        instance_id: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[StoredIntent]:
        if not 1 <= limit <= 10_000:
            raise ValueError("intent limit must be in [1, 10000]")
        query = """SELECT * FROM strategy_intents
        WHERE (? IS NULL OR instance_id=?)
        AND (? IS NULL OR symbol=?)
        ORDER BY storage_sequence DESC LIMIT ?"""
        parameters = (instance_id, instance_id, symbol, symbol, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._stored_intent_from_row(row) for row in rows]

    def metrics(self) -> dict[str, int]:
        with self._connect() as connection:
            checkpoints = int(
                connection.execute("SELECT count(*) FROM strategy_checkpoints").fetchone()[0]
            )
            evaluations = int(
                connection.execute("SELECT count(*) FROM strategy_evaluations").fetchone()[0]
            )
            rows = connection.execute(
                """SELECT delivery_state,count(*) FROM strategy_intents
                GROUP BY delivery_state"""
            ).fetchall()
            counts = {str(row[0]): int(row[1]) for row in rows}
            intents = sum(counts.values())
            latest = int(
                connection.execute(
                    "SELECT coalesce(max(storage_sequence),0) FROM strategy_intents"
                ).fetchone()[0]
            )
        return {
            "checkpoints": checkpoints,
            "evaluations": evaluations,
            "intents": intents,
            "pending_intents": counts.get(StrategyDeliveryState.PENDING.value, 0),
            "published_intents": counts.get(StrategyDeliveryState.PUBLISHED.value, 0),
            "suppressed_intents": counts.get(StrategyDeliveryState.SUPPRESSED.value, 0),
            "latest_intent_sequence": latest,
        }
