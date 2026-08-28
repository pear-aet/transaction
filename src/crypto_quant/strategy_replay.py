from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .strategy_contracts import StrategyIntentFilter
from .strategy_store import (
    StoredIntent,
    StrategyDeliveryState,
    StrategyStore,
)


class StrategyConsumerFilterMismatch(RuntimeError):
    """Raised when a durable consumer name is reused with another filter."""


@dataclass(frozen=True, slots=True)
class DurableIntentFilter:
    intent_filter: StrategyIntentFilter = StrategyIntentFilter()
    delivery_states: frozenset[StrategyDeliveryState] = frozenset({StrategyDeliveryState.PUBLISHED})

    def __post_init__(self) -> None:
        if not self.delivery_states:
            raise ValueError("durable intent filter requires at least one delivery state")
        if StrategyDeliveryState.PENDING in self.delivery_states:
            raise ValueError("durable intent replay cannot include mutable pending state")

    def matches(self, value: StoredIntent) -> bool:
        return value.delivery_state in self.delivery_states and self.intent_filter.matches(
            value.intent
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent_filter.to_dict(),
            "delivery_states": sorted(item.value for item in self.delivery_states),
        }


@dataclass(frozen=True, slots=True)
class StrategyConsumerOffset:
    consumer: str
    sequence: int
    filter_hash: str
    updated_at: int


@dataclass(frozen=True, slots=True)
class StrategyReplayBatch:
    consumer: str
    filter_hash: str
    start_sequence: int
    high_watermark: int
    scanned_through: int
    intents: tuple[StoredIntent, ...]
    skipped: int


def durable_filter_hash(value: DurableIntentFilter) -> str:
    canonical = json.dumps(
        value.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StrategyReplayLedger:
    """Pinned durable cursors over the immutable strategy-intent sequence."""

    def __init__(self, store: StrategyStore | str | Path) -> None:
        self.store = store if isinstance(store, StrategyStore) else StrategyStore(store)
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
                """CREATE TABLE IF NOT EXISTS strategy_consumer_offsets (
                    consumer TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL CHECK(sequence >= 0),
                    filter_hash TEXT NOT NULL,
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0)
                )"""
            )

    def offset(
        self,
        consumer: str,
        durable_filter: DurableIntentFilter,
    ) -> StrategyConsumerOffset:
        if not consumer:
            raise ValueError("consumer must not be empty")
        signature = durable_filter_hash(durable_filter)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_consumer_offsets WHERE consumer=?",
                (consumer,),
            ).fetchone()
        if row is None:
            return StrategyConsumerOffset(consumer, 0, signature, 0)
        if str(row["filter_hash"]) != signature:
            raise StrategyConsumerFilterMismatch(
                f"durable strategy consumer filter changed; use a new name: {consumer}"
            )
        return StrategyConsumerOffset(
            consumer,
            int(row["sequence"]),
            str(row["filter_hash"]),
            int(row["updated_at"]),
        )

    def read_batch(
        self,
        consumer: str,
        durable_filter: DurableIntentFilter | None = None,
        *,
        limit: int = 1000,
    ) -> StrategyReplayBatch:
        selected_filter = durable_filter or DurableIntentFilter()
        offset = self.offset(consumer, selected_filter)
        high_watermark = self.store.latest_intent_sequence()
        rows = self.store.load_intents(
            after_sequence=offset.sequence,
            through_sequence=high_watermark,
            limit=limit,
        )
        scanned_through = offset.sequence
        matched = []
        skipped = 0
        for item in rows:
            if item.delivery_state is StrategyDeliveryState.PENDING:
                break
            storage_sequence = item.intent.storage_sequence
            if storage_sequence is None:
                raise RuntimeError("stored strategy intent has no storage sequence")
            scanned_through = int(storage_sequence)
            if selected_filter.matches(item):
                matched.append(item)
            else:
                skipped += 1
        return StrategyReplayBatch(
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
        batch: StrategyReplayBatch,
        *,
        updated_at: int,
    ) -> StrategyConsumerOffset:
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
        intent_sequences = [item.intent.storage_sequence for item in batch.intents]
        if any(sequence is None or sequence <= 0 for sequence in intent_sequences):
            raise ValueError("replay batch intents require positive storage sequences")
        normalized_sequences = [
            int(sequence) for sequence in intent_sequences if sequence is not None
        ]
        if normalized_sequences != sorted(set(normalized_sequences)):
            raise ValueError("replay batch intents are duplicate or out of order")
        if any(
            sequence <= batch.start_sequence or sequence > batch.scanned_through
            for sequence in normalized_sequences
        ):
            raise ValueError("replay batch intent sequence falls outside the scanned range")
        with self._lock, self._connect() as connection:
            latest = self.store.latest_intent_sequence()
            if batch.high_watermark > latest or batch.scanned_through > latest:
                raise ValueError("consumer offset cannot exceed the strategy intent log")
            row = connection.execute(
                "SELECT * FROM strategy_consumer_offsets WHERE consumer=?",
                (batch.consumer,),
            ).fetchone()
            if row is not None:
                if str(row["filter_hash"]) != batch.filter_hash:
                    raise StrategyConsumerFilterMismatch(
                        f"durable strategy consumer filter changed: {batch.consumer}"
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
                raise ValueError("new durable consumer must start at sequence zero")
            connection.execute(
                """INSERT INTO strategy_consumer_offsets(
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
        return StrategyConsumerOffset(
            batch.consumer,
            batch.scanned_through,
            batch.filter_hash,
            updated_at,
        )

    def metrics(self) -> dict[str, int]:
        with self._connect() as connection:
            consumers = int(
                connection.execute("SELECT count(*) FROM strategy_consumer_offsets").fetchone()[0]
            )
        return {
            "durable_consumers": consumers,
            "latest_intent_sequence": self.store.latest_intent_sequence(),
        }
