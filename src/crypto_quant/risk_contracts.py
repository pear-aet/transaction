from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from .config import StrategyConfig
from .data import timeframe_ms
from .domain import MarketRules, RiskBracket, finite_json
from .strategy_contracts import StrategyAction, StrategyIntent


class RiskDecisionStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RiskApprovedAction(StrEnum):
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"


class RiskReason(StrEnum):
    APPROVED_ENTRY = "approved_entry"
    APPROVED_EXIT = "approved_exit"
    ACCOUNT_GLOBAL_HALT = "account_global_halt"
    ACCOUNT_DAY_HALT = "account_day_halt"
    ACCOUNT_DRAWDOWN_HALT = "account_drawdown_halt"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    ACCOUNT_DRAWDOWN_LIMIT = "account_drawdown_limit"
    CONSECUTIVE_LOSS_LIMIT = "consecutive_loss_limit"
    STALE_INTENT = "stale_intent"
    STALE_ACCOUNT = "stale_account"
    STALE_MARKET = "stale_market"
    FUTURE_SNAPSHOT = "future_snapshot"
    REFERENCE_DEVIATION = "reference_deviation"
    NON_POSITIVE_EQUITY = "non_positive_equity"
    INSUFFICIENT_AVAILABLE_BALANCE = "insufficient_available_balance"
    SYMBOL_POSITION_EXISTS = "symbol_position_exists"
    NO_OPEN_POSITION = "no_open_position"
    INVALID_STOP = "invalid_stop"
    SYMBOL_RISK_LIMIT = "symbol_risk_limit"
    PORTFOLIO_RISK_LIMIT = "portfolio_risk_limit"
    GROUP_RISK_LIMIT = "group_risk_limit"
    GROSS_NOTIONAL_LIMIT = "gross_notional_limit"
    DIRECTIONAL_NOTIONAL_LIMIT = "directional_notional_limit"
    MARKET_MINIMUM = "market_minimum"
    LEVERAGE_LIMIT = "leverage_limit"
    LIQUIDATION_BUFFER = "liquidation_buffer"
    INVALID_MARKET_RULES = "invalid_market_rules"


def _finite(value: float, name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _validate_strict_json(value: Any, path: str = "evidence") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        _finite(value, path)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            _validate_strict_json(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_strict_json(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains unsupported type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class PositionRiskSnapshot:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    mark_price: float
    initial_risk: float
    maintenance_margin_rate: float
    correlation_group: str = "default"
    stop_price: float | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.correlation_group:
            raise ValueError("position symbol and correlation_group must not be empty")
        if self.side not in {"long", "short"}:
            raise ValueError("position side must be long or short")
        for name, value in (
            ("quantity", self.quantity),
            ("entry_price", self.entry_price),
            ("mark_price", self.mark_price),
            ("initial_risk", self.initial_risk),
            ("maintenance_margin_rate", self.maintenance_margin_rate),
        ):
            _finite(value, name)
        if min(self.quantity, self.entry_price, self.mark_price) <= 0:
            raise ValueError("position quantity and prices must be positive")
        if self.initial_risk < 0:
            raise ValueError("position initial_risk must be non-negative")
        if not 0 < self.maintenance_margin_rate < 1:
            raise ValueError("invalid position maintenance margin rate")
        if self.stop_price is not None:
            _finite(self.stop_price, "stop_price")
            if self.stop_price <= 0:
                raise ValueError("position stop_price must be positive")

    @property
    def notional(self) -> float:
        return self.quantity * self.mark_price

    @property
    def signed_notional(self) -> float:
        return self.notional if self.side == "long" else -self.notional

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AccountRiskSnapshot:
    snapshot_id: str
    account_id: str
    observed_at: int
    equity: float
    available_balance: float
    maintenance_margin: float
    day_start_equity: float
    peak_equity: float
    consecutive_losses: int
    positions: tuple[PositionRiskSnapshot, ...] = ()
    global_halt: bool = False
    day_halt: bool = False
    drawdown_halt: bool = False
    sequence: int = 0

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.account_id:
            raise ValueError("account snapshot identifiers must not be empty")
        if min(self.observed_at, self.consecutive_losses, self.sequence) < 0:
            raise ValueError("account snapshot counters must be non-negative")
        for name, value in (
            ("equity", self.equity),
            ("available_balance", self.available_balance),
            ("maintenance_margin", self.maintenance_margin),
            ("day_start_equity", self.day_start_equity),
            ("peak_equity", self.peak_equity),
        ):
            _finite(value, name)
        if self.maintenance_margin < 0:
            raise ValueError("maintenance_margin must be non-negative")
        symbols = [position.symbol for position in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("account snapshot contains duplicate symbol positions")

    @property
    def open_risk(self) -> float:
        return sum(position.initial_risk for position in self.positions)

    @property
    def gross_notional(self) -> float:
        return sum(position.notional for position in self.positions)

    def symbol_risk(self, symbol: str) -> float:
        return sum(
            position.initial_risk for position in self.positions if position.symbol == symbol
        )

    def group_risk(self, correlation_group: str) -> float:
        return sum(
            position.initial_risk
            for position in self.positions
            if position.correlation_group == correlation_group
        )

    def directional_notional(self, side: str) -> float:
        return sum(position.notional for position in self.positions if position.side == side)

    def position(self, symbol: str) -> PositionRiskSnapshot | None:
        return next((position for position in self.positions if position.symbol == symbol), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "account_id": self.account_id,
            "observed_at": self.observed_at,
            "equity": self.equity,
            "available_balance": self.available_balance,
            "maintenance_margin": self.maintenance_margin,
            "day_start_equity": self.day_start_equity,
            "peak_equity": self.peak_equity,
            "consecutive_losses": self.consecutive_losses,
            "positions": [position.to_dict() for position in self.positions],
            "global_halt": self.global_halt,
            "day_halt": self.day_halt,
            "drawdown_halt": self.drawdown_halt,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AccountRiskSnapshot:
        return cls(
            snapshot_id=str(value["snapshot_id"]),
            account_id=str(value["account_id"]),
            observed_at=int(value["observed_at"]),
            equity=float(value["equity"]),
            available_balance=float(value["available_balance"]),
            maintenance_margin=float(value["maintenance_margin"]),
            day_start_equity=float(value["day_start_equity"]),
            peak_equity=float(value["peak_equity"]),
            consecutive_losses=int(value["consecutive_losses"]),
            positions=tuple(
                PositionRiskSnapshot(**position) for position in value.get("positions", [])
            ),
            global_halt=bool(value.get("global_halt", False)),
            day_halt=bool(value.get("day_halt", False)),
            drawdown_halt=bool(value.get("drawdown_halt", False)),
            sequence=int(value.get("sequence", 0)),
        )


@dataclass(frozen=True, slots=True)
class MarketRiskSnapshot:
    snapshot_id: str
    symbol: str
    observed_at: int
    mark_price: float
    rules: MarketRules
    correlation_group: str = "default"
    volatility_annualized: float | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.symbol or not self.correlation_group:
            raise ValueError("market snapshot identifiers must not be empty")
        if min(self.observed_at, self.sequence) < 0:
            raise ValueError("market snapshot counters must be non-negative")
        _finite(self.mark_price, "mark_price")
        if self.mark_price <= 0:
            raise ValueError("mark_price must be positive")
        if self.rules.symbol != self.symbol:
            raise ValueError("market rules symbol does not match market snapshot")
        if self.volatility_annualized is not None:
            _finite(self.volatility_annualized, "volatility_annualized")
            if self.volatility_annualized <= 0:
                raise ValueError("volatility_annualized must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "observed_at": self.observed_at,
            "mark_price": self.mark_price,
            "rules": {
                "symbol": self.rules.symbol,
                "tick_size": self.rules.tick_size,
                "step_size": self.rules.step_size,
                "min_quantity": self.rules.min_quantity,
                "min_notional": self.rules.min_notional,
                "max_leverage": self.rules.max_leverage,
                "brackets": [asdict(bracket) for bracket in self.rules.brackets],
            },
            "correlation_group": self.correlation_group,
            "volatility_annualized": self.volatility_annualized,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MarketRiskSnapshot:
        raw_rules = dict(value["rules"])
        rules = MarketRules(
            symbol=str(raw_rules["symbol"]),
            tick_size=float(raw_rules["tick_size"]),
            step_size=float(raw_rules["step_size"]),
            min_quantity=float(raw_rules["min_quantity"]),
            min_notional=float(raw_rules["min_notional"]),
            max_leverage=float(raw_rules["max_leverage"]),
            brackets=tuple(RiskBracket(**bracket) for bracket in raw_rules["brackets"]),
        )
        return cls(
            snapshot_id=str(value["snapshot_id"]),
            symbol=str(value["symbol"]),
            observed_at=int(value["observed_at"]),
            mark_price=float(value["mark_price"]),
            rules=rules,
            correlation_group=str(value.get("correlation_group", "default")),
            volatility_annualized=(
                None
                if value.get("volatility_annualized") is None
                else float(value["volatility_annualized"])
            ),
            sequence=int(value.get("sequence", 0)),
        )


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    policy_id: str = "cross-margin-default"
    version: str = "1.0.0"
    risk_per_trade: float = 0.01
    max_symbol_risk: float = 0.015
    max_portfolio_risk: float = 0.03
    max_group_risk: float = 0.02
    max_gross_notional_multiplier: float = 40.0
    max_directional_notional_multiplier: float = 20.0
    max_leverage: float = 100.0
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    liquidation_fee_rate: float = 0.005
    min_liquidation_buffer: float = 0.02
    max_reference_deviation: float = 0.02
    max_intent_age_ms: int = 1_800_000
    max_account_age_ms: int = 30_000
    max_market_age_ms: int = 30_000
    daily_loss_limit: float = 0.03
    max_account_drawdown: float = 0.25
    max_consecutive_losses: int = 5
    volatility_target_annualized: float = 0.80
    min_volatility_risk_scale: float = 0.25

    def __post_init__(self) -> None:
        if not self.policy_id or not self.version:
            raise ValueError("risk policy identifiers must not be empty")
        if not 0 < self.risk_per_trade <= self.max_symbol_risk <= self.max_portfolio_risk <= 0.2:
            raise ValueError("risk budgets are inconsistent")
        if not 0 < self.max_group_risk <= self.max_portfolio_risk:
            raise ValueError("max_group_risk must be within the portfolio limit")
        if not 0 < self.max_directional_notional_multiplier <= self.max_gross_notional_multiplier:
            raise ValueError("notional multipliers are inconsistent")
        if not 1 <= self.max_leverage <= 100:
            raise ValueError("max_leverage must be in [1, 100]")
        for name, value in (
            ("fee_rate", self.fee_rate),
            ("slippage_rate", self.slippage_rate),
            ("liquidation_fee_rate", self.liquidation_fee_rate),
            ("min_liquidation_buffer", self.min_liquidation_buffer),
            ("max_reference_deviation", self.max_reference_deviation),
            ("daily_loss_limit", self.daily_loss_limit),
            ("max_account_drawdown", self.max_account_drawdown),
            ("min_volatility_risk_scale", self.min_volatility_risk_scale),
        ):
            _finite(value, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if min(self.max_intent_age_ms, self.max_account_age_ms, self.max_market_age_ms) < 0:
            raise ValueError("risk policy age limits must be non-negative")
        if not 1 <= self.max_consecutive_losses <= 100:
            raise ValueError("max_consecutive_losses must be in [1, 100]")
        _finite(self.volatility_target_annualized, "volatility_target_annualized")
        if self.volatility_target_annualized <= 0:
            raise ValueError("volatility_target_annualized must be positive")
        if self.min_volatility_risk_scale <= 0:
            raise ValueError("min_volatility_risk_scale must be positive")

    @classmethod
    def from_strategy_config(cls, config: StrategyConfig) -> RiskPolicy:
        return cls(
            risk_per_trade=config.risk_per_trade,
            max_symbol_risk=config.max_symbol_risk,
            max_portfolio_risk=config.max_portfolio_risk,
            max_group_risk=min(config.max_portfolio_risk, max(config.max_symbol_risk, 0.02)),
            max_gross_notional_multiplier=min(100.0, config.max_directional_notional * 2),
            max_directional_notional_multiplier=config.max_directional_notional,
            max_leverage=config.max_leverage,
            fee_rate=config.fee_rate,
            slippage_rate=config.slippage_rate,
            liquidation_fee_rate=config.liquidation_fee_rate,
            min_liquidation_buffer=config.min_liquidation_buffer,
            max_intent_age_ms=2 * timeframe_ms(config.low_timeframe),
            daily_loss_limit=config.daily_loss_limit,
            max_account_drawdown=config.max_account_drawdown,
            max_consecutive_losses=config.max_consecutive_losses,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def risk_policy_hash(policy: RiskPolicy) -> str:
    canonical = json.dumps(
        policy.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RiskContext:
    engine_instance_id: str
    intent: StrategyIntent
    account: AccountRiskSnapshot
    market: MarketRiskSnapshot
    policy: RiskPolicy
    evaluated_at: int

    def __post_init__(self) -> None:
        if not self.engine_instance_id:
            raise ValueError("risk engine_instance_id must not be empty")
        if self.evaluated_at < 0:
            raise ValueError("evaluated_at must be non-negative")
        if self.intent.symbol != self.market.symbol:
            raise ValueError("strategy intent and market snapshot symbols differ")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: str
    engine_instance_id: str
    policy_id: str
    policy_version: str
    policy_hash: str
    account_id: str
    account_snapshot_id: str
    market_snapshot_id: str
    intent_id: str
    intent_storage_sequence: int | None
    symbol: str
    requested_action: StrategyAction
    status: RiskDecisionStatus
    approved_action: RiskApprovedAction | None
    decision_time: int
    evaluated_at: int
    reasons: tuple[RiskReason, ...]
    reference_price: float
    approval_price: float
    stop_price: float | None
    quantity: float
    notional: float
    leverage: float
    required_margin: float
    reduce_only: bool
    risk_budget: float
    initial_risk: float
    symbol_risk_after: float
    portfolio_risk_after: float
    group_risk_after: float
    gross_notional_after: float
    directional_notional_after: float
    maintenance_margin_after: float
    liquidation_buffer_ratio: float
    evidence: dict[str, Any]
    sequence: int = 0
    storage_sequence: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        text_values = (
            self.decision_id,
            self.engine_instance_id,
            self.policy_id,
            self.policy_version,
            self.policy_hash,
            self.account_id,
            self.account_snapshot_id,
            self.market_snapshot_id,
            self.intent_id,
            self.symbol,
        )
        if any(not value for value in text_values):
            raise ValueError("risk decision text fields must not be empty")
        counters = (self.decision_time, self.evaluated_at, self.sequence)
        if min(counters) < 0:
            raise ValueError("risk decision counters must be non-negative")
        if self.intent_storage_sequence is not None and self.intent_storage_sequence <= 0:
            raise ValueError("intent_storage_sequence must be positive")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("risk decision storage_sequence must be positive")
        if self.schema_version <= 0:
            raise ValueError("risk decision schema_version must be positive")
        if not self.reasons:
            raise ValueError("risk decision requires at least one reason")
        numeric = (
            self.reference_price,
            self.approval_price,
            self.quantity,
            self.notional,
            self.leverage,
            self.required_margin,
            self.risk_budget,
            self.initial_risk,
            self.symbol_risk_after,
            self.portfolio_risk_after,
            self.group_risk_after,
            self.gross_notional_after,
            self.directional_notional_after,
            self.maintenance_margin_after,
            self.liquidation_buffer_ratio,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("risk decision contains a non-finite number")
        if min(self.reference_price, self.approval_price) <= 0:
            raise ValueError("risk decision prices must be positive")
        if self.stop_price is not None:
            _finite(self.stop_price, "stop_price")
            if self.stop_price <= 0:
                raise ValueError("risk decision stop_price must be positive")
        nonnegative = (
            self.quantity,
            self.notional,
            self.leverage,
            self.required_margin,
            self.risk_budget,
            self.initial_risk,
            self.symbol_risk_after,
            self.portfolio_risk_after,
            self.group_risk_after,
            self.gross_notional_after,
            self.directional_notional_after,
            self.maintenance_margin_after,
        )
        if min(nonnegative) < 0:
            raise ValueError("risk decision amounts must be non-negative")
        if self.status is RiskDecisionStatus.APPROVED:
            if self.approved_action is None or self.quantity <= 0 or self.notional <= 0:
                raise ValueError("approved risk decision requires an executable plan")
            if self.leverage < 1:
                raise ValueError("approved risk decision requires valid leverage")
            opening = self.approved_action in {
                RiskApprovedAction.OPEN_LONG,
                RiskApprovedAction.OPEN_SHORT,
            }
            if opening:
                if self.reduce_only or self.required_margin <= 0 or self.stop_price is None:
                    raise ValueError(
                        "approved entry requires margin, stop and non-reduce-only plan"
                    )
            elif not self.reduce_only or self.required_margin != 0 or self.stop_price is not None:
                raise ValueError("approved exit must be reduce-only without new margin or stop")
        else:
            if self.approved_action is not None or self.reduce_only:
                raise ValueError("rejected risk decision cannot contain an approved action")
            if any(value != 0 for value in (self.quantity, self.notional, self.leverage)):
                raise ValueError("rejected risk decision cannot contain order sizing")
        _validate_strict_json(self.evidence)
        json.dumps(self.evidence, sort_keys=True, allow_nan=False)

    def with_sequence(self, sequence: int) -> RiskDecision:
        if sequence <= 0:
            raise ValueError("published risk sequence must be positive")
        return replace(self, sequence=sequence)

    def with_storage_sequence(self, sequence: int) -> RiskDecision:
        if sequence <= 0:
            raise ValueError("risk storage sequence must be positive")
        return replace(self, storage_sequence=sequence)

    def semantic_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                "decision_id": self.decision_id,
                "engine_instance_id": self.engine_instance_id,
                "policy_id": self.policy_id,
                "policy_version": self.policy_version,
                "policy_hash": self.policy_hash,
                "account_id": self.account_id,
                "account_snapshot_id": self.account_snapshot_id,
                "market_snapshot_id": self.market_snapshot_id,
                "intent_id": self.intent_id,
                "intent_storage_sequence": self.intent_storage_sequence,
                "symbol": self.symbol,
                "requested_action": self.requested_action.value,
                "status": self.status.value,
                "approved_action": (
                    None if self.approved_action is None else self.approved_action.value
                ),
                "decision_time": self.decision_time,
                "reasons": [reason.value for reason in self.reasons],
                "reference_price": self.reference_price,
                "approval_price": self.approval_price,
                "stop_price": self.stop_price,
                "quantity": self.quantity,
                "notional": self.notional,
                "leverage": self.leverage,
                "required_margin": self.required_margin,
                "reduce_only": self.reduce_only,
                "risk_budget": self.risk_budget,
                "initial_risk": self.initial_risk,
                "symbol_risk_after": self.symbol_risk_after,
                "portfolio_risk_after": self.portfolio_risk_after,
                "group_risk_after": self.group_risk_after,
                "gross_notional_after": self.gross_notional_after,
                "directional_notional_after": self.directional_notional_after,
                "maintenance_margin_after": self.maintenance_margin_after,
                "liquidation_buffer_ratio": self.liquidation_buffer_ratio,
                "evidence": self.evidence,
                "schema_version": self.schema_version,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                **self.semantic_dict(),
                "evaluated_at": self.evaluated_at,
                "sequence": self.sequence,
                "storage_sequence": self.storage_sequence,
            }
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RiskDecision:
        return cls(
            decision_id=str(value["decision_id"]),
            engine_instance_id=str(value["engine_instance_id"]),
            policy_id=str(value["policy_id"]),
            policy_version=str(value["policy_version"]),
            policy_hash=str(value["policy_hash"]),
            account_id=str(value["account_id"]),
            account_snapshot_id=str(value["account_snapshot_id"]),
            market_snapshot_id=str(value["market_snapshot_id"]),
            intent_id=str(value["intent_id"]),
            intent_storage_sequence=(
                None
                if value.get("intent_storage_sequence") is None
                else int(value["intent_storage_sequence"])
            ),
            symbol=str(value["symbol"]),
            requested_action=StrategyAction(value["requested_action"]),
            status=RiskDecisionStatus(value["status"]),
            approved_action=(
                None
                if value.get("approved_action") is None
                else RiskApprovedAction(value["approved_action"])
            ),
            decision_time=int(value["decision_time"]),
            evaluated_at=int(value["evaluated_at"]),
            reasons=tuple(RiskReason(reason) for reason in value["reasons"]),
            reference_price=float(value["reference_price"]),
            approval_price=float(value["approval_price"]),
            stop_price=(None if value.get("stop_price") is None else float(value["stop_price"])),
            quantity=float(value["quantity"]),
            notional=float(value["notional"]),
            leverage=float(value["leverage"]),
            required_margin=float(value["required_margin"]),
            reduce_only=bool(value["reduce_only"]),
            risk_budget=float(value["risk_budget"]),
            initial_risk=float(value["initial_risk"]),
            symbol_risk_after=float(value["symbol_risk_after"]),
            portfolio_risk_after=float(value["portfolio_risk_after"]),
            group_risk_after=float(value["group_risk_after"]),
            gross_notional_after=float(value["gross_notional_after"]),
            directional_notional_after=float(value["directional_notional_after"]),
            maintenance_margin_after=float(value["maintenance_margin_after"]),
            liquidation_buffer_ratio=float(value["liquidation_buffer_ratio"]),
            evidence=dict(value.get("evidence") or {}),
            sequence=int(value.get("sequence", 0)),
            storage_sequence=(
                None if value.get("storage_sequence") is None else int(value["storage_sequence"])
            ),
            schema_version=int(value.get("schema_version", 1)),
        )


@dataclass(frozen=True, slots=True)
class RiskDecisionFilter:
    engine_instance_ids: frozenset[str] | None = None
    account_ids: frozenset[str] | None = None
    symbols: frozenset[str] | None = None
    statuses: frozenset[RiskDecisionStatus] | None = None
    actions: frozenset[RiskApprovedAction] | None = None

    def matches(self, decision: RiskDecision) -> bool:
        if (
            self.engine_instance_ids is not None
            and decision.engine_instance_id not in self.engine_instance_ids
        ):
            return False
        if self.account_ids is not None and decision.account_id not in self.account_ids:
            return False
        if self.symbols is not None and decision.symbol not in self.symbols:
            return False
        if self.statuses is not None and decision.status not in self.statuses:
            return False
        return self.actions is None or decision.approved_action in self.actions
