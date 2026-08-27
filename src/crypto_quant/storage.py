from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .domain import Candle, FundingRate, finite_json
from .engine import EngineState


class Repository:
    """Small transactional SQLite repository for market data, jobs and engine state."""

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
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, price_type TEXT NOT NULL,
                    timestamp INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
                    low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
                    PRIMARY KEY(symbol, timeframe, price_type, timestamp)
                );
                CREATE TABLE IF NOT EXISTS funding_rates (
                    symbol TEXT NOT NULL, timestamp INTEGER NOT NULL, rate REAL NOT NULL,
                    PRIMARY KEY(symbol, timestamp)
                );
                CREATE TABLE IF NOT EXISTS engine_states (
                    account_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, timestamp INTEGER NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_account_time
                    ON events(account_id, timestamp);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    request TEXT NOT NULL, result TEXT, error TEXT,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS strategy_versions (
                    symbol TEXT NOT NULL, strategy_id TEXT NOT NULL, version TEXT NOT NULL,
                    parameters TEXT NOT NULL, metrics TEXT NOT NULL, valid_until INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(symbol, strategy_id, version)
                );
                """
            )

    def upsert_candles(
        self, symbol: str, timeframe: str, candles: list[Candle], price_type: str = "contract"
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.executemany(
                """INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol,timeframe,price_type,timestamp) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,
                close=excluded.close,volume=excluded.volume""",
                [
                    (
                        symbol,
                        timeframe,
                        price_type,
                        item.timestamp,
                        item.open,
                        item.high,
                        item.low,
                        item.close,
                        item.volume,
                    )
                    for item in candles
                ],
            )

    def load_candles(
        self,
        symbol: str,
        timeframe: str,
        since: int = 0,
        price_type: str = "contract",
    ) -> list[Candle]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT timestamp,open,high,low,close,volume FROM candles
                WHERE symbol=? AND timeframe=? AND price_type=? AND timestamp>=?
                ORDER BY timestamp""",
                (symbol, timeframe, price_type, since),
            ).fetchall()
        return [Candle(*row) for row in rows]

    def upsert_funding(self, symbol: str, rates: list[FundingRate]) -> None:
        with self._lock, self._connect() as connection:
            connection.executemany(
                """INSERT INTO funding_rates VALUES (?, ?, ?)
                ON CONFLICT(symbol,timestamp) DO UPDATE SET rate=excluded.rate""",
                [(symbol, item.timestamp, item.rate) for item in rates],
            )

    def load_funding(self, symbol: str, since: int = 0) -> list[FundingRate]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT timestamp,rate FROM funding_rates
                WHERE symbol=? AND timestamp>=? ORDER BY timestamp""",
                (symbol, since),
            ).fetchall()
        return [FundingRate(*row) for row in rows]

    def save_engine_state(self, account_id: str, state: EngineState, timestamp: int) -> None:
        payload = json.dumps(state.to_dict(), ensure_ascii=False, allow_nan=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO engine_states VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                payload=excluded.payload,updated_at=excluded.updated_at""",
                (account_id, payload, timestamp),
            )

    def load_engine_state(self, account_id: str, initial_cash: float) -> EngineState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM engine_states WHERE account_id=?", (account_id,)
            ).fetchone()
        return (
            EngineState(initial_cash) if row is None else EngineState.from_dict(json.loads(row[0]))
        )

    def append_event(
        self,
        account_id: str,
        timestamp: int,
        kind: str,
        payload: dict[str, Any],
        event_id: str | None = None,
    ) -> str:
        identifier = event_id or str(uuid.uuid4())
        encoded = json.dumps(finite_json(payload), ensure_ascii=False, allow_nan=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?)",
                (identifier, account_id, timestamp, kind, encoded),
            )
        return identifier

    def create_job(self, kind: str, request: dict[str, Any], now_ms: int) -> str:
        identifier = str(uuid.uuid4())
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO jobs(id,kind,status,request,created_at,updated_at)
                VALUES(?,?,?,?,?,?)""",
                (identifier, kind, "queued", json.dumps(request), now_ms, now_ms),
            )
        return identifier

    def update_job(
        self,
        identifier: str,
        status: str,
        now_ms: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        encoded = json.dumps(finite_json(result), allow_nan=False) if result is not None else None
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?,result=?,error=?,updated_at=? WHERE id=?",
                (status, encoded, error, now_ms, identifier),
            )

    def get_job(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for key in ("request", "result"):
            if value.get(key):
                value[key] = json.loads(value[key])
        value["cancel_requested"] = bool(value["cancel_requested"])
        return value

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("job limit must be in [1, 100]")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            for key in ("request", "result"):
                if value.get(key):
                    value[key] = json.loads(value[key])
            value["cancel_requested"] = bool(value["cancel_requested"])
            output.append(value)
        return output

    def cancel_job(self, identifier: str, now_ms: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE jobs SET cancel_requested=1,updated_at=?
                WHERE id=? AND status IN ('queued','running')""",
                (now_ms, identifier),
            )
        return cursor.rowcount == 1

    def job_cancelled(self, identifier: str) -> bool:
        job = self.get_job(identifier)
        return bool(job and job["cancel_requested"])

    def metrics(self) -> dict[str, int]:
        with self._connect() as connection:
            jobs = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT status,count(*) FROM jobs GROUP BY status"
                ).fetchall()
            }
            candles = connection.execute("SELECT count(*) FROM candles").fetchone()[0]
            events = connection.execute("SELECT count(*) FROM events").fetchone()[0]
        return {
            "candles": int(candles),
            "events": int(events),
            **{f"jobs_{key}": int(value) for key, value in jobs.items()},
        }

    def save_strategy_version(
        self,
        symbol: str,
        strategy_id: str,
        version: str,
        parameters: dict[str, Any],
        metrics: dict[str, Any],
        valid_until: int | None,
        enabled: bool,
    ) -> None:
        with self._lock, self._connect() as connection:
            if enabled:
                connection.execute(
                    "UPDATE strategy_versions SET enabled=0 WHERE symbol=?", (symbol,)
                )
            connection.execute(
                """INSERT INTO strategy_versions VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol,strategy_id,version) DO UPDATE SET
                parameters=excluded.parameters,metrics=excluded.metrics,
                valid_until=excluded.valid_until,enabled=excluded.enabled""",
                (
                    symbol,
                    strategy_id,
                    version,
                    json.dumps(finite_json(parameters), allow_nan=False),
                    json.dumps(finite_json(metrics), allow_nan=False),
                    valid_until,
                    int(enabled),
                ),
            )

    def list_strategy_versions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM strategy_versions"
        parameters: tuple[str, ...] = ()
        if symbol:
            query += " WHERE symbol=?"
            parameters = (symbol,)
        query += " ORDER BY symbol,enabled DESC,strategy_id,version"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        output = []
        for row in rows:
            value = dict(row)
            value["parameters"] = json.loads(value["parameters"])
            value["metrics"] = json.loads(value["metrics"])
            value["enabled"] = bool(value["enabled"])
            output.append(value)
        return output
