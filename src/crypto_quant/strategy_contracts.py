from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .config import StrategyConfig
from .domain import Signal, finite_json
from .market_events import MarketEvent, MarketEventKind
from .strategy import StrategyDecision, StrategySpec


class StrategyAction(StrEnum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"


def _validate_strict_json(value: Any, path: str = "evidence") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            _validate_strict_json(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_strict_json(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains an unsupported value type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class StrategyIntent:
    intent_id: str
    strategy_id: str
    strategy_version: str
    instance_id: str
    symbol: str
    action: StrategyAction
    decision_time: int
    created_at: int
    reference_price: float
    stop_price: float | None
    reason: str
    source_event_id: str
    source_sequence: int
    high_timeframe: str
    low_timeframe: str
    parameter_hash: str
    evidence: dict[str, Any]
    sequence: int = 0
    storage_sequence: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        text_values = (
            self.intent_id,
            self.strategy_id,
            self.strategy_version,
            self.instance_id,
            self.symbol,
            self.reason,
            self.source_event_id,
            self.high_timeframe,
            self.low_timeframe,
            self.parameter_hash,
        )
        if any(not value for value in text_values):
            raise ValueError("strategy intent text fields must not be empty")
        if min(self.decision_time, self.created_at, self.source_sequence, self.sequence) < 0:
            raise ValueError("strategy intent timestamps and sequences must be non-negative")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("strategy intent storage_sequence must be positive")
        if self.schema_version <= 0:
            raise ValueError("strategy intent schema_version must be positive")
        if not math.isfinite(self.reference_price) or self.reference_price <= 0:
            raise ValueError("reference_price must be finite and positive")
        if self.stop_price is not None and (
            not math.isfinite(self.stop_price) or self.stop_price <= 0
        ):
            raise ValueError("stop_price must be finite and positive")
        if self.action is StrategyAction.ENTER_LONG:
            if self.stop_price is None or self.stop_price >= self.reference_price:
                raise ValueError("long entry requires a stop below the reference price")
        elif self.action is StrategyAction.ENTER_SHORT:
            if self.stop_price is None or self.stop_price <= self.reference_price:
                raise ValueError("short entry requires a stop above the reference price")
        elif self.action is StrategyAction.EXIT and self.stop_price is not None:
            raise ValueError("exit intent must not contain a stop price")
        _validate_strict_json(self.evidence)
        json.dumps(self.evidence, sort_keys=True, allow_nan=False)

    def with_sequence(self, sequence: int) -> StrategyIntent:
        if sequence <= 0:
            raise ValueError("published strategy sequence must be positive")
        return replace(self, sequence=sequence)

    def with_storage_sequence(self, sequence: int) -> StrategyIntent:
        if sequence <= 0:
            raise ValueError("strategy storage sequence must be positive")
        return replace(self, storage_sequence=sequence)

    def semantic_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                "intent_id": self.intent_id,
                "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
                "instance_id": self.instance_id,
                "symbol": self.symbol,
                "action": self.action.value,
                "decision_time": self.decision_time,
                "reference_price": self.reference_price,
                "stop_price": self.stop_price,
                "reason": self.reason,
                "source_event_id": self.source_event_id,
                "high_timeframe": self.high_timeframe,
                "low_timeframe": self.low_timeframe,
                "parameter_hash": self.parameter_hash,
                "evidence": self.evidence,
                "schema_version": self.schema_version,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                **self.semantic_dict(),
                "created_at": self.created_at,
                "source_sequence": self.source_sequence,
                "sequence": self.sequence,
                "storage_sequence": self.storage_sequence,
            }
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StrategyIntent:
        return cls(
            intent_id=str(value["intent_id"]),
            strategy_id=str(value["strategy_id"]),
            strategy_version=str(value["strategy_version"]),
            instance_id=str(value["instance_id"]),
            symbol=str(value["symbol"]),
            action=StrategyAction(value["action"]),
            decision_time=int(value["decision_time"]),
            created_at=int(value["created_at"]),
            reference_price=float(value["reference_price"]),
            stop_price=(None if value.get("stop_price") is None else float(value["stop_price"])),
            reason=str(value["reason"]),
            source_event_id=str(value["source_event_id"]),
            source_sequence=int(value.get("source_sequence", 0)),
            high_timeframe=str(value["high_timeframe"]),
            low_timeframe=str(value["low_timeframe"]),
            parameter_hash=str(value["parameter_hash"]),
            evidence=dict(value.get("evidence") or {}),
            sequence=int(value.get("sequence", 0)),
            storage_sequence=(
                None
                if value.get("storage_sequence") is None
                else int(value["storage_sequence"])
            ),
            schema_version=int(value.get("schema_version", 1)),
        )


@dataclass(frozen=True, slots=True)
class StrategyIntentFilter:
    strategy_ids: frozenset[str] | None = None
    instance_ids: frozenset[str] | None = None
    symbols: frozenset[str] | None = None
    actions: frozenset[StrategyAction] | None = None

    def matches(self, intent: StrategyIntent) -> bool:
        if self.strategy_ids is not None and intent.strategy_id not in self.strategy_ids:
            return False
        if self.instance_ids is not None and intent.instance_id not in self.instance_ids:
            return False
        if self.symbols is not None and intent.symbol not in self.symbols:
            return False
        return self.actions is None or intent.action in self.actions

    def to_dict(self) -> dict[str, list[str] | None]:
        return {
            "strategy_ids": (
                None if self.strategy_ids is None else sorted(self.strategy_ids)
            ),
            "instance_ids": (
                None if self.instance_ids is None else sorted(self.instance_ids)
            ),
            "symbols": None if self.symbols is None else sorted(self.symbols),
            "actions": (
                None if self.actions is None else sorted(item.value for item in self.actions)
            ),
        }


def strategy_parameter_payload(config: StrategyConfig) -> dict[str, Any]:
    return {
        "entry_model": config.entry_model,
        "entry_lookback": config.entry_lookback,
        "min_stroke_separation": config.min_stroke_separation,
        "stop_buffer": config.stop_buffer,
        "high_timeframe": config.high_timeframe,
        "low_timeframe": config.low_timeframe,
    }


def strategy_parameter_hash(config: StrategyConfig) -> str:
    canonical = json.dumps(
        strategy_parameter_payload(config),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _intent_id(
    *,
    spec: StrategySpec,
    instance_id: str,
    symbol: str,
    action: StrategyAction,
    decision_time: int,
    reference_price: float,
    stop_price: float | None,
    reason: str,
    source_event_id: str,
    parameter_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "strategy_id": spec.strategy_id,
            "strategy_version": spec.version,
            "instance_id": instance_id,
            "symbol": symbol,
            "action": action.value,
            "decision_time": decision_time,
            "reference_price": format(reference_price, ".17g"),
            "stop_price": None if stop_price is None else format(stop_price, ".17g"),
            "reason": reason,
            "source_event_id": source_event_id,
            "parameter_hash": parameter_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.blake2s(canonical.encode("utf-8"), digest_size=16).hexdigest()
    return f"strategy_intent:{digest}"


def _evidence(decision: StrategyDecision) -> dict[str, Any]:
    high = decision.high_snapshot
    low = decision.low_snapshot
    latest_point = low.points[-1] if low.points else None
    return {
        "high_trend": high.trend.value,
        "high": {
            "normalized_candles": len(high.candles),
            "fractals": len(high.fractals),
            "strokes": len(high.strokes),
            "pivots": len(high.pivots),
            "segments": len(high.segments),
        },
        "low": {
            "normalized_candles": len(low.candles),
            "fractals": len(low.fractals),
            "strokes": len(low.strokes),
            "pivots": len(low.pivots),
            "segments": len(low.segments),
            "latest_point": (
                None
                if latest_point is None
                else {
                    "side": latest_point.side,
                    "point_type": latest_point.point_type,
                    "confirmed_at": latest_point.confirmed_at,
                }
            ),
        },
    }


def strategy_intent_from_signal(
    signal: Signal,
    decision: StrategyDecision,
    config: StrategyConfig,
    spec: StrategySpec,
    source_event: MarketEvent,
    *,
    instance_id: str,
    created_at: int,
) -> StrategyIntent:
    if source_event.kind is not MarketEventKind.CANDLE_CLOSED:
        raise ValueError("strategy intents must originate from a closed-candle event")
    if source_event.symbol != config.symbol or source_event.timeframe != config.low_timeframe:
        raise ValueError("source event does not match the strategy symbol and low timeframe")
    if source_event.price_type != "contract":
        raise ValueError("strategy decisions require contract-price candles")
    action = StrategyAction(signal.action)
    payload = source_event.payload.to_dict()
    if signal.timestamp != int(payload["open_time"]):
        raise ValueError("strategy signal timestamp must match the source candle open time")
    reference_price = float(payload["close"])
    parameter_hash = strategy_parameter_hash(config)
    intent_id = _intent_id(
        spec=spec,
        instance_id=instance_id,
        symbol=config.symbol,
        action=action,
        decision_time=source_event.event_time,
        reference_price=reference_price,
        stop_price=signal.stop_price,
        reason=signal.reason,
        source_event_id=source_event.event_id,
        parameter_hash=parameter_hash,
    )
    return StrategyIntent(
        intent_id=intent_id,
        strategy_id=spec.strategy_id,
        strategy_version=spec.version,
        instance_id=instance_id,
        symbol=config.symbol,
        action=action,
        decision_time=source_event.event_time,
        created_at=created_at,
        reference_price=reference_price,
        stop_price=signal.stop_price,
        reason=signal.reason,
        source_event_id=source_event.event_id,
        source_sequence=source_event.sequence,
        high_timeframe=config.high_timeframe,
        low_timeframe=config.low_timeframe,
        parameter_hash=parameter_hash,
        evidence=_evidence(decision),
    )
