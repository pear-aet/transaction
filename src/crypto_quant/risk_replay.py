from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .risk_contracts import RiskDecisionFilter
from .risk_store import RiskDeliveryState, RiskStore, StoredRiskDecision


class RiskConsumerFilterMismatch(RuntimeError):
    """Raised when a durable risk consumer changes its pinned filter."""


@dataclass(frozen=True, slots=True)
class DurableRiskFilter:
    decision_filter: RiskDecisionFilter = RiskDecisionFilter()
    delivery_states: frozenset[RiskDeliveryState] = frozenset({RiskDeliveryState.PUBLISHED})

    def __post_init__(self) -> None:
        if not self.delivery_states:
            raise ValueError("durable risk filter requires at least one delivery state")
        if RiskDeliveryState.PENDING in self.delivery_states:
            raise ValueError("durable risk replay cannot include mutable pending state")

    def matches(self, value: StoredRiskDecision) -> bool:
        return value.delivery_state in self.delivery_states and self.decision_filter.matches(
            value.decision
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": {
                "engine_instance_ids": (
                    None
                    if self.decision_filter.engine_instance_ids is None
                    else sorted(self.decision_filter.engine_instance_ids)
                ),
                "account_ids": (
                    None
                    if self.decision_filter.account_ids is None
                    else sorted(self.decision_filter.account_ids)
                ),
                "symbols": (
                    None
                    if self.decision_filter.symbols is None
                    else sorted(self.decision_filter.symbols)
                ),
                "statuses": (
                    None
                    if self.decision_filter.statuses is None
                    else sorted(item.value for item in self.decision_filter.statuses)
                ),
                "actions": (
                    None
                    if self.decision_filter.actions is None
                    else sorted(item.value for item in self.decision_filter.actions)
                ),
            },
            "delivery_states": sorted(item.value for item in self.delivery_states),
        }


@dataclass(frozen=True, slots=True)
class RiskConsumerOffset:
    consumer: str
    sequence: int
    filter_hash: str
    updated_at: int


@dataclass(frozen=True, slots=True)
class RiskReplayBatch:
    consumer: str
    filter_hash: str
    start_sequence: int
    high_watermark: int
    scanned_through: int
    decisions: tuple[StoredRiskDecision, ...]
    skipped: int


def durable_risk_filter_hash(value: DurableRiskFilter) -> str:
    canonical = json.dumps(
        value.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RiskReplayLedger:
    """Pinned durable cursors over immutable risk-decision storage sequences."""

    def __init__(self, store: RiskStore | str | Path) -> None:
        self.store = store if isinstance(store, RiskStore) else RiskStore(store)
        self.path = self.store.path
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS risk_consumer_offsets (
                    consumer TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL CHECK(sequence >= 0),
                    filter_hash TEXT NOT NULL,
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0)
                )"""
            )

    def offset(
        self,
        consumer: str,
        durable_filter: DurableRiskFilter,
    ) -> RiskConsumerOffset:
        if not consumer:
            raise ValueError("consumer must not be empty")
        signature = durable_risk_filter_hash(durable_filter)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM risk_consumer_offsets WHERE consumer=?",
                (consumer,),
            ).fetchone()
        if row is None:
            return RiskConsumerOffset(consumer, 0, signature, 0)
        if str(row["filter_hash"]) != signature:
            raise RiskConsumerFilterMismatch(
                f"durable risk consumer filter changed; use a new name: {consumer}"
            )
        return RiskConsumerOffset(
            consumer,
            int(row["sequence"]),
            str(row["filter_hash"]),
            int(row["updated_at"]),
        )

    def read_batch(
        self,
        consumer: str,
        durable_filter: DurableRiskFilter | None = None,
        *,
        limit: int = 1000,
    ) -> RiskReplayBatch:
        selected_filter = durable_filter or DurableRiskFilter()
        offset = self.offset(consumer, selected_filter)
        high_watermark = self.store.latest_decision_sequence()
        rows = self.store.load_decisions(
            after_sequence=offset.sequence,
            through_sequence=high_watermark,
            limit=limit,
        )
        scanned_through = offset.sequence
        matched = []
        skipped = 0
        for item in rows:
            if item.delivery_state is RiskDeliveryState.PENDING:
                break
            storage_sequence = item.decision.storage_sequence
            if storage_sequence is None:
                raise RuntimeError("stored risk decision has no storage sequence")
            scanned_through = int(storage_sequence)
            if selected_filter.matches(item):
                matched.append(item)
            else:
                skipped += 1
        return RiskReplayBatch(
            consumer,
            offset.filter_hash,
            offset.sequence,
            high_watermark,
            scanned_through,
            tuple(matched),
            skipped,
        )

    def commit_batch(
        self,
        batch: RiskReplayBatch,
        *,
        updated_at: int,
    ) -> RiskConsumerOffset:
        if updated_at < 0:
            raise ValueError("updated_at must be non-negative")
        if not batch.consumer or not batch.filter_hash:
            raise ValueError("replay batch identifiers must not be empty")
        if batch.start_sequence < 0:
            raise ValueError("replay batch start_sequence must be non-negative")
        if batch.high_watermark < batch.start_sequence:
            raise ValueError("replay batch high_watermark cannot precede its start")
        if not batch.start_sequence <= batch.scanned_through <= batch.high_watermark:
            raise ValueError("replay batch scanned_through must remain within its snapshot")
        if batch.skipped < 0:
            raise ValueError("replay batch skipped count must be non-negative")
        sequences = [item.decision.storage_sequence for item in batch.decisions]
        if any(sequence is None or sequence <= 0 for sequence in sequences):
            raise ValueError("replay batch decisions require positive storage sequences")
        normalized = [int(sequence) for sequence in sequences if sequence is not None]
        if normalized != sorted(set(normalized)):
            raise ValueError("replay batch decisions are duplicate or out of order")
        if any(
            sequence <= batch.start_sequence or sequence > batch.scanned_through
            for sequence in normalized
        ):
            raise ValueError("replay decision sequence falls outside the scanned range")

        with self._lock, self._connect() as connection:
            latest = self.store.latest_decision_sequence()
            if batch.high_watermark > latest or batch.scanned_through > latest:
                raise ValueError("consumer offset cannot exceed the risk decision log")
            row = connection.execute(
                "SELECT * FROM risk_consumer_offsets WHERE consumer=?",
                (batch.consumer,),
            ).fetchone()
            if row is not None:
                if str(row["filter_hash"]) != batch.filter_hash:
                    raise RiskConsumerFilterMismatch(
                        f"durable risk consumer filter changed: {batch.consumer}"
                    )
                current = int(row["sequence"])
                if batch.start_sequence != current:
                    raise ValueError(
                        f"stale replay batch for {batch.consumer}: "
                        f"expected start {current}, got {batch.start_sequence}"
                    )
                if batch.scanned_through < current:
                    raise ValueError("consumer offset cannot move backwards")
            elif batch.start_sequence != 0:
                raise ValueError("new durable risk consumer must start at sequence zero")
            connection.execute(
                """INSERT INTO risk_consumer_offsets(
                    consumer,sequence,filter_hash,updated_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(consumer) DO UPDATE SET
                    sequence=excluded.sequence,
                    updated_at=excluded.updated_at""",
                (
                    batch.consumer,
                    batch.scanned_through,
                    batch.filter_hash,
                    updated_at,
                ),
            )
        return RiskConsumerOffset(
            batch.consumer,
            batch.scanned_through,
            batch.filter_hash,
            updated_at,
        )

    def metrics(self) -> dict[str, int]:
        with self._connect() as connection:
            consumers = int(
                connection.execute("SELECT count(*) FROM risk_consumer_offsets").fetchone()[0]
            )
        return {
            "durable_consumers": consumers,
            "latest_decision_sequence": self.store.latest_decision_sequence(),
        }
