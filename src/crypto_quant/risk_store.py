from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .risk_contracts import RiskDecision, RiskDecisionStatus


class RiskDecisionConflict(RuntimeError):
    """Raised when one intent resolves to different immutable risk semantics."""


class RiskInstanceError(RuntimeError):
    """Raised when a risk-engine instance changes account or policy identity."""


class RiskDeliveryState(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    RECORDED = "recorded"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class RiskInstance:
    engine_instance_id: str
    account_id: str
    policy_id: str
    policy_version: str
    policy_hash: str
    updated_at: int


@dataclass(frozen=True, slots=True)
class RiskEvaluation:
    engine_instance_id: str
    intent_id: str
    intent_storage_sequence: int | None
    decision_id: str
    account_snapshot_id: str
    market_snapshot_id: str
    evaluated_at: int


@dataclass(frozen=True, slots=True)
class StoredRiskDecision:
    decision: RiskDecision
    delivery_state: RiskDeliveryState
    published_at: int | None
    expired_at: int | None


@dataclass(frozen=True, slots=True)
class RiskCommit:
    instance: RiskInstance
    duplicate: bool
    decision_inserted: bool
    stored_decision: StoredRiskDecision


class RiskStore:
    """Atomic risk evaluations plus a durable executable-decision outbox."""

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
                CREATE TABLE IF NOT EXISTS risk_instances (
                    engine_instance_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0)
                );

                CREATE TABLE IF NOT EXISTS risk_decisions (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL UNIQUE,
                    engine_instance_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    intent_storage_sequence INTEGER,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('approved','rejected')),
                    approved_action TEXT,
                    payload TEXT NOT NULL,
                    delivery_state TEXT NOT NULL CHECK(
                        delivery_state IN ('pending','published','recorded','expired')
                    ),
                    published_at INTEGER,
                    expired_at INTEGER,
                    UNIQUE(engine_instance_id, intent_id),
                    FOREIGN KEY(engine_instance_id)
                        REFERENCES risk_instances(engine_instance_id)
                );
                CREATE INDEX IF NOT EXISTS risk_decisions_outbox
                    ON risk_decisions(delivery_state, storage_sequence);
                CREATE INDEX IF NOT EXISTS risk_decisions_account_sequence
                    ON risk_decisions(account_id, storage_sequence);

                CREATE TABLE IF NOT EXISTS risk_evaluations (
                    engine_instance_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    intent_storage_sequence INTEGER,
                    decision_id TEXT NOT NULL,
                    account_snapshot_id TEXT NOT NULL,
                    market_snapshot_id TEXT NOT NULL,
                    evaluated_at INTEGER NOT NULL CHECK(evaluated_at >= 0),
                    PRIMARY KEY(engine_instance_id, intent_id),
                    UNIQUE(engine_instance_id, intent_storage_sequence),
                    FOREIGN KEY(engine_instance_id)
                        REFERENCES risk_instances(engine_instance_id),
                    FOREIGN KEY(decision_id) REFERENCES risk_decisions(decision_id)
                );
                CREATE INDEX IF NOT EXISTS risk_evaluations_sequence
                    ON risk_evaluations(engine_instance_id, intent_storage_sequence);
                """
            )

    @staticmethod
    def _instance_from_row(row: sqlite3.Row) -> RiskInstance:
        return RiskInstance(
            str(row["engine_instance_id"]),
            str(row["account_id"]),
            str(row["policy_id"]),
            str(row["policy_version"]),
            str(row["policy_hash"]),
            int(row["updated_at"]),
        )

    @staticmethod
    def _evaluation_from_row(row: sqlite3.Row) -> RiskEvaluation:
        return RiskEvaluation(
            str(row["engine_instance_id"]),
            str(row["intent_id"]),
            None if row["intent_storage_sequence"] is None else int(row["intent_storage_sequence"]),
            str(row["decision_id"]),
            str(row["account_snapshot_id"]),
            str(row["market_snapshot_id"]),
            int(row["evaluated_at"]),
        )

    @staticmethod
    def _encode_decision(decision: RiskDecision) -> str:
        payload = decision.to_dict()
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
    def _decode_decision(payload: str, storage_sequence: int) -> RiskDecision:
        return RiskDecision.from_dict(json.loads(payload)).with_storage_sequence(storage_sequence)

    @classmethod
    def _stored_from_row(cls, row: sqlite3.Row) -> StoredRiskDecision:
        return StoredRiskDecision(
            cls._decode_decision(str(row["payload"]), int(row["storage_sequence"])),
            RiskDeliveryState(row["delivery_state"]),
            None if row["published_at"] is None else int(row["published_at"]),
            None if row["expired_at"] is None else int(row["expired_at"]),
        )

    @staticmethod
    def _same_semantics(left: RiskDecision, right: RiskDecision) -> bool:
        return left.semantic_dict() == right.semantic_dict()

    @staticmethod
    def _expected_delivery_state(decision: RiskDecision) -> RiskDeliveryState:
        return (
            RiskDeliveryState.PENDING
            if decision.status is RiskDecisionStatus.APPROVED
            else RiskDeliveryState.RECORDED
        )

    @staticmethod
    def _assert_instance(instance: RiskInstance, decision: RiskDecision) -> None:
        expected = (
            decision.account_id,
            decision.policy_id,
            decision.policy_version,
            decision.policy_hash,
        )
        actual = (
            instance.account_id,
            instance.policy_id,
            instance.policy_version,
            instance.policy_hash,
        )
        if actual != expected:
            raise RiskInstanceError(
                "risk policy or account changed for an existing engine instance; "
                "use a new engine_instance_id"
            )

    def load_instance(self, engine_instance_id: str) -> RiskInstance | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM risk_instances WHERE engine_instance_id=?",
                (engine_instance_id,),
            ).fetchone()
        return None if row is None else self._instance_from_row(row)

    def load_evaluation(self, engine_instance_id: str, intent_id: str) -> RiskEvaluation | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM risk_evaluations
                WHERE engine_instance_id=? AND intent_id=?""",
                (engine_instance_id, intent_id),
            ).fetchone()
        return None if row is None else self._evaluation_from_row(row)

    def _load_stored(
        self,
        connection: sqlite3.Connection,
        decision_id: str,
    ) -> StoredRiskDecision | None:
        row = connection.execute(
            "SELECT * FROM risk_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        return None if row is None else self._stored_from_row(row)

    def load_by_intent(
        self,
        engine_instance_id: str,
        intent_id: str,
    ) -> StoredRiskDecision | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM risk_decisions
                WHERE engine_instance_id=? AND intent_id=?""",
                (engine_instance_id, intent_id),
            ).fetchone()
        return None if row is None else self._stored_from_row(row)

    def commit_evaluation(
        self,
        decision: RiskDecision,
        *,
        updated_at: int,
    ) -> RiskCommit:
        if updated_at < 0:
            raise ValueError("updated_at must be non-negative")
        if decision.sequence != 0 or decision.storage_sequence is not None:
            raise ValueError("only unsequenced, unstored risk decisions may be committed")
        expected_state = self._expected_delivery_state(decision)

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM risk_instances WHERE engine_instance_id=?",
                (decision.engine_instance_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO risk_instances(
                        engine_instance_id,account_id,policy_id,policy_version,
                        policy_hash,updated_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        decision.engine_instance_id,
                        decision.account_id,
                        decision.policy_id,
                        decision.policy_version,
                        decision.policy_hash,
                        updated_at,
                    ),
                )
                instance = RiskInstance(
                    decision.engine_instance_id,
                    decision.account_id,
                    decision.policy_id,
                    decision.policy_version,
                    decision.policy_hash,
                    updated_at,
                )
            else:
                instance = self._instance_from_row(row)
                self._assert_instance(instance, decision)
                connection.execute(
                    "UPDATE risk_instances SET updated_at=? WHERE engine_instance_id=?",
                    (updated_at, decision.engine_instance_id),
                )
                instance = RiskInstance(
                    instance.engine_instance_id,
                    instance.account_id,
                    instance.policy_id,
                    instance.policy_version,
                    instance.policy_hash,
                    updated_at,
                )

            evaluation_row = connection.execute(
                """SELECT * FROM risk_evaluations
                WHERE engine_instance_id=? AND intent_id=?""",
                (decision.engine_instance_id, decision.intent_id),
            ).fetchone()
            if evaluation_row is not None:
                evaluation = self._evaluation_from_row(evaluation_row)
                stored = self._load_stored(connection, evaluation.decision_id)
                if stored is None:
                    raise RiskDecisionConflict(
                        "risk evaluation exists without its immutable decision"
                    )
                if evaluation.decision_id != decision.decision_id:
                    raise RiskDecisionConflict(
                        f"risk decision changed for intent {decision.intent_id}: "
                        f"{evaluation.decision_id} != {decision.decision_id}"
                    )
                if not self._same_semantics(stored.decision, decision):
                    raise RiskDecisionConflict(
                        f"risk decision semantics changed: {decision.decision_id}"
                    )
                allowed_states = (
                    {
                        RiskDeliveryState.PENDING,
                        RiskDeliveryState.PUBLISHED,
                        RiskDeliveryState.EXPIRED,
                    }
                    if decision.status is RiskDecisionStatus.APPROVED
                    else {RiskDeliveryState.RECORDED}
                )
                if stored.delivery_state not in allowed_states:
                    raise RiskDecisionConflict(
                        f"risk decision delivery disposition changed: {decision.decision_id}"
                    )
                return RiskCommit(instance, True, False, stored)

            encoded = self._encode_decision(decision)
            cursor = connection.execute(
                """INSERT OR IGNORE INTO risk_decisions(
                    decision_id,engine_instance_id,account_id,intent_id,
                    intent_storage_sequence,symbol,status,approved_action,payload,
                    delivery_state,published_at,expired_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
                (
                    decision.decision_id,
                    decision.engine_instance_id,
                    decision.account_id,
                    decision.intent_id,
                    decision.intent_storage_sequence,
                    decision.symbol,
                    decision.status.value,
                    None if decision.approved_action is None else decision.approved_action.value,
                    encoded,
                    expected_state.value,
                ),
            )
            inserted = cursor.rowcount == 1
            stored = self._load_stored(connection, decision.decision_id)
            if stored is None:
                competing_row = connection.execute(
                    """SELECT * FROM risk_decisions
                    WHERE engine_instance_id=? AND intent_id=?""",
                    (decision.engine_instance_id, decision.intent_id),
                ).fetchone()
                if competing_row is None:
                    raise RuntimeError("risk decision insert completed without a readable row")
                competing = self._stored_from_row(competing_row)
                raise RiskDecisionConflict(
                    f"multiple risk decisions for one strategy intent: "
                    f"{competing.decision.decision_id} != {decision.decision_id}"
                )
            if not self._same_semantics(stored.decision, decision):
                raise RiskDecisionConflict(f"risk decision ID collision: {decision.decision_id}")
            if stored.delivery_state is not expected_state:
                raise RiskDecisionConflict(
                    f"risk decision delivery disposition changed: {decision.decision_id}"
                )

            connection.execute(
                """INSERT INTO risk_evaluations(
                    engine_instance_id,intent_id,intent_storage_sequence,
                    decision_id,account_snapshot_id,market_snapshot_id,evaluated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    decision.engine_instance_id,
                    decision.intent_id,
                    decision.intent_storage_sequence,
                    decision.decision_id,
                    decision.account_snapshot_id,
                    decision.market_snapshot_id,
                    decision.evaluated_at,
                ),
            )
        return RiskCommit(instance, False, inserted, stored)

    def pending_decisions(
        self,
        *,
        engine_instance_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[RiskDecision]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("decision limit must be in [1, 10000]")
        query = """SELECT * FROM risk_decisions
        WHERE delivery_state='pending'
        AND storage_sequence>?
        AND (? IS NULL OR engine_instance_id=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (after_sequence, engine_instance_id, engine_instance_id, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._stored_from_row(row).decision for row in rows]

    def mark_published(self, decision_id: str, published_at: int) -> bool:
        if not decision_id or published_at < 0:
            raise ValueError("invalid risk publication marker")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM risk_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"risk decision not found: {decision_id}")
            stored = self._stored_from_row(row)
            state = stored.delivery_state
            if state is RiskDeliveryState.PUBLISHED:
                return False
            if state is not RiskDeliveryState.PENDING:
                raise ValueError(f"{state.value} risk decision cannot be published")
            if published_at > stored.decision.valid_until:
                raise ValueError("risk decision expired before publication")
            connection.execute(
                """UPDATE risk_decisions
                SET delivery_state='published',published_at=?
                WHERE decision_id=?""",
                (published_at, decision_id),
            )
        return True

    def mark_expired(self, decision_id: str, expired_at: int) -> bool:
        if not decision_id or expired_at < 0:
            raise ValueError("invalid risk expiry marker")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM risk_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"risk decision not found: {decision_id}")
            stored = self._stored_from_row(row)
            state = stored.delivery_state
            if state is RiskDeliveryState.EXPIRED:
                return False
            if state is not RiskDeliveryState.PENDING:
                raise ValueError(f"{state.value} risk decision cannot expire")
            if expired_at <= stored.decision.valid_until:
                raise ValueError("risk decision cannot expire before validity window ends")
            connection.execute(
                """UPDATE risk_decisions
                SET delivery_state='expired',expired_at=?
                WHERE decision_id=?""",
                (expired_at, decision_id),
            )
        return True

    def load_decisions(
        self,
        *,
        after_sequence: int = 0,
        through_sequence: int | None = None,
        engine_instance_id: str | None = None,
        account_id: str | None = None,
        symbol: str | None = None,
        limit: int = 1000,
    ) -> list[StoredRiskDecision]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("decision limit must be in [1, 10000]")
        if through_sequence is not None and through_sequence < after_sequence:
            return []
        query = """SELECT * FROM risk_decisions
        WHERE storage_sequence>?
        AND (? IS NULL OR storage_sequence<=?)
        AND (? IS NULL OR engine_instance_id=?)
        AND (? IS NULL OR account_id=?)
        AND (? IS NULL OR symbol=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (
            after_sequence,
            through_sequence,
            through_sequence,
            engine_instance_id,
            engine_instance_id,
            account_id,
            account_id,
            symbol,
            symbol,
            limit,
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._stored_from_row(row) for row in rows]

    def latest_decision_sequence(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT coalesce(max(storage_sequence),0) FROM risk_decisions"
            ).fetchone()
        return int(row[0])

    def list_decisions(
        self,
        *,
        engine_instance_id: str | None = None,
        account_id: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[StoredRiskDecision]:
        if not 1 <= limit <= 10_000:
            raise ValueError("decision limit must be in [1, 10000]")
        query = """SELECT * FROM risk_decisions
        WHERE (? IS NULL OR engine_instance_id=?)
        AND (? IS NULL OR account_id=?)
        AND (? IS NULL OR symbol=?)
        ORDER BY storage_sequence DESC LIMIT ?"""
        parameters = (
            engine_instance_id,
            engine_instance_id,
            account_id,
            account_id,
            symbol,
            symbol,
            limit,
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._stored_from_row(row) for row in rows]

    def metrics(self) -> dict[str, int]:
        with self._connect() as connection:
            instances = int(connection.execute("SELECT count(*) FROM risk_instances").fetchone()[0])
            evaluations = int(
                connection.execute("SELECT count(*) FROM risk_evaluations").fetchone()[0]
            )
            rows = connection.execute(
                """SELECT delivery_state,count(*) FROM risk_decisions
                GROUP BY delivery_state"""
            ).fetchall()
            counts = {str(row[0]): int(row[1]) for row in rows}
            decisions = sum(counts.values())
            latest = int(
                connection.execute(
                    "SELECT coalesce(max(storage_sequence),0) FROM risk_decisions"
                ).fetchone()[0]
            )
        return {
            "instances": instances,
            "evaluations": evaluations,
            "decisions": decisions,
            "pending_decisions": counts.get(RiskDeliveryState.PENDING.value, 0),
            "published_decisions": counts.get(RiskDeliveryState.PUBLISHED.value, 0),
            "recorded_decisions": counts.get(RiskDeliveryState.RECORDED.value, 0),
            "expired_decisions": counts.get(RiskDeliveryState.EXPIRED.value, 0),
            "latest_decision_sequence": latest,
        }
