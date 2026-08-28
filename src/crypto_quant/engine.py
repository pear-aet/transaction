from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from .config import StrategyConfig
from .data import timeframe_ms
from .domain import Candle, FundingRate, MarketRules, RiskBracket, Signal, finite_json
from .risk import evaluate_risk
from .risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    PositionRiskSnapshot,
    RiskApprovedAction,
    RiskContext,
    RiskDecision,
    RiskDecisionStatus,
    RiskPolicy,
)
from .strategy import decide, strategy_spec
from .strategy_contracts import StrategyAction, StrategyIntent, strategy_parameter_hash

FUNDING_INTERVAL_MS = 28_800_000


def _stable_identifier(prefix: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        finite_json(payload),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.blake2s(canonical.encode("utf-8"), digest_size=16).hexdigest()
    return f"{prefix}:{digest}"


@dataclass(slots=True)
class Position:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_price: float
    take_profit: float
    entry_time: int
    entry_fee: float
    effective_leverage: float
    initial_risk: float
    maintenance_margin_rate: float


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    side: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    quantity: float
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float
    exit_reason: str
    effective_leverage: float


@dataclass(slots=True)
class EngineState:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    pending: dict[str, Signal] = field(default_factory=dict)
    funding_accrued: dict[str, float] = field(default_factory=dict)
    applied_funding: dict[str, list[int]] = field(default_factory=dict)
    next_fallback_funding: dict[str, int] = field(default_factory=dict)
    latest_prices: dict[str, float] = field(default_factory=dict)
    last_timestamps: dict[str, int] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    peak_equity: float | None = None
    day_id: int | None = None
    day_start_equity: float | None = None
    consecutive_losses: int = 0
    day_paused: bool = False
    drawdown_halted: bool = False
    global_stop: bool = False
    warnings: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                "cash": self.cash,
                "positions": {key: asdict(value) for key, value in self.positions.items()},
                "pending": {key: asdict(value) for key, value in self.pending.items()},
                "funding_accrued": self.funding_accrued,
                "applied_funding": self.applied_funding,
                "next_fallback_funding": self.next_fallback_funding,
                "latest_prices": self.latest_prices,
                "last_timestamps": self.last_timestamps,
                "trades": [asdict(value) for value in self.trades],
                "events": self.events[-5000:],
                "equity_curve": self.equity_curve,
                "peak_equity": self.peak_equity,
                "day_id": self.day_id,
                "day_start_equity": self.day_start_equity,
                "consecutive_losses": self.consecutive_losses,
                "day_paused": self.day_paused,
                "drawdown_halted": self.drawdown_halted,
                "global_stop": self.global_stop,
                "warnings": sorted(self.warnings),
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EngineState:
        return cls(
            cash=float(data["cash"]),
            positions={key: Position(**value) for key, value in data.get("positions", {}).items()},
            pending={key: Signal(**value) for key, value in data.get("pending", {}).items()},
            funding_accrued={
                key: float(value) for key, value in data.get("funding_accrued", {}).items()
            },
            applied_funding={
                key: [int(item) for item in value]
                for key, value in data.get("applied_funding", {}).items()
            },
            next_fallback_funding={
                key: int(value) for key, value in data.get("next_fallback_funding", {}).items()
            },
            latest_prices={
                key: float(value) for key, value in data.get("latest_prices", {}).items()
            },
            last_timestamps={
                key: int(value) for key, value in data.get("last_timestamps", {}).items()
            },
            trades=[Trade(**value) for value in data.get("trades", [])],
            events=list(data.get("events", [])),
            equity_curve=[tuple(item) for item in data.get("equity_curve", [])],
            peak_equity=data.get("peak_equity"),
            day_id=data.get("day_id"),
            day_start_equity=data.get("day_start_equity"),
            consecutive_losses=int(data.get("consecutive_losses", 0)),
            day_paused=bool(data.get("day_paused", False)),
            drawdown_halted=bool(data.get("drawdown_halted", False)),
            global_stop=bool(data.get("global_stop", False)),
            warnings=set(data.get("warnings", [])),
        )


def default_market_rules(config: StrategyConfig, symbol: str | None = None) -> MarketRules:
    return MarketRules(
        symbol or config.symbol,
        tick_size=1e-8,
        step_size=1e-8,
        min_quantity=0.0,
        min_notional=0.0,
        max_leverage=config.max_leverage,
        brackets=(RiskBracket(0.0, 1e30, config.maintenance_margin_rate, 0.0),),
    )


def _floor(value: float, step: float) -> float:
    return float(
        (Decimal(str(value)) / Decimal(str(step))).to_integral_value(ROUND_DOWN)
        * Decimal(str(step))
    )


def _round_price(value: float, tick: float) -> float:
    return float(
        (Decimal(str(value)) / Decimal(str(tick))).to_integral_value(ROUND_HALF_UP)
        * Decimal(str(tick))
    )


def _pnl(position: Position, price: float) -> float:
    sign = 1 if position.side == "long" else -1
    return sign * position.quantity * (price - position.entry_price)


def _bracket(rules: MarketRules, notional: float) -> RiskBracket:
    for item in rules.brackets:
        if item.notional_floor <= notional <= item.notional_cap:
            return item
    return rules.brackets[-1]


class EventEngine:
    """Shared deterministic event engine used by backtests and paper trading."""

    def __init__(
        self,
        config: StrategyConfig,
        state: EngineState | None = None,
        market_rules: dict[str, MarketRules] | None = None,
        decision_function: Callable | None = None,
        risk_evaluator: Callable[[RiskContext], RiskDecision] | None = None,
    ) -> None:
        self.config = config
        self.state = state or EngineState(config.initial_cash)
        self.rules = market_rules or {config.symbol: default_market_rules(config)}
        self.decision_function = decision_function or decide
        self.risk_evaluator = risk_evaluator or evaluate_risk
        if self.state.peak_equity is None:
            self.state.peak_equity = self.state.cash

    def equity(self, overrides: dict[str, float] | None = None) -> float:
        prices = self.state.latest_prices | (overrides or {})
        value = self.state.cash
        for symbol, position in self.state.positions.items():
            value += _pnl(position, prices.get(symbol, position.entry_price))
            value -= self.state.funding_accrued.get(symbol, 0.0)
        return value

    def maintenance_requirement(self, overrides: dict[str, float] | None = None) -> float:
        prices = self.state.latest_prices | (overrides or {})
        total = 0.0
        for symbol, position in self.state.positions.items():
            rules = self.rules.get(symbol, default_market_rules(self.config, symbol))
            notional = position.quantity * prices.get(symbol, position.entry_price)
            bracket = _bracket(rules, notional)
            total += max(
                0.0, notional * bracket.maintenance_margin_rate - bracket.maintenance_amount
            )
        return total

    def _record(self, timestamp: int, kind: str, **payload: Any) -> None:
        self.state.events.append(finite_json({"timestamp": timestamp, "kind": kind, **payload}))

    def _close(self, symbol: str, raw_price: float, timestamp: int, reason: str) -> None:
        position = self.state.positions[symbol]
        slip = (
            1 - self.config.slippage_rate
            if position.side == "long"
            else 1 + self.config.slippage_rate
        )
        price = raw_price * slip
        fee_rate = (
            self.config.liquidation_fee_rate if reason == "liquidation" else self.config.fee_rate
        )
        exit_fee = position.quantity * price * fee_rate
        gross = _pnl(position, price)
        funding = self.state.funding_accrued.get(symbol, 0.0)
        net = gross - position.entry_fee - exit_fee - funding
        self.state.cash += gross - exit_fee - funding
        trade = Trade(
            symbol,
            position.side,
            position.entry_time,
            timestamp,
            position.entry_price,
            price,
            position.quantity,
            gross,
            position.entry_fee + exit_fee,
            funding,
            net,
            reason,
            position.effective_leverage,
        )
        self.state.trades.append(trade)
        self.state.consecutive_losses = self.state.consecutive_losses + 1 if net < 0 else 0
        del self.state.positions[symbol]
        self.state.funding_accrued.pop(symbol, None)
        self._record(timestamp, "position_closed", symbol=symbol, reason=reason, net_pnl=net)

    def _entry_risk_intent(
        self,
        symbol: str,
        signal: Signal,
        bar: Candle,
    ) -> StrategyIntent:
        action = StrategyAction(signal.action)
        spec = strategy_spec(self.config.entry_model)
        source_payload = {
            "symbol": symbol,
            "decision_time": bar.timestamp,
            "action": action.value,
            "signal_timestamp": signal.timestamp,
            "reason": signal.reason,
            "stop_price": signal.stop_price,
        }
        source_event_id = _stable_identifier("event_engine_signal", source_payload)
        intent_payload = {
            **source_payload,
            "source_event_id": source_event_id,
            "reference_price": bar.open,
            "parameter_hash": strategy_parameter_hash(self.config),
        }
        return StrategyIntent(
            intent_id=_stable_identifier("event_engine_intent", intent_payload),
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            instance_id=f"event-engine:{symbol}",
            symbol=symbol,
            action=action,
            decision_time=bar.timestamp,
            created_at=bar.timestamp,
            reference_price=bar.open,
            stop_price=signal.stop_price,
            reason=signal.reason,
            source_event_id=source_event_id,
            source_sequence=0,
            high_timeframe=self.config.high_timeframe,
            low_timeframe=self.config.low_timeframe,
            parameter_hash=strategy_parameter_hash(self.config),
            evidence={"adapter": "EventEngine"},
        )

    def _entry_account_snapshot(self, symbol: str, bar: Candle) -> AccountRiskSnapshot:
        prices = self.state.latest_prices | {symbol: bar.open}
        equity = self.equity({symbol: bar.open})
        maintenance = self.maintenance_requirement({symbol: bar.open})
        positions: list[PositionRiskSnapshot] = []
        for key in sorted(self.state.positions):
            position = self.state.positions[key]
            mark_price = prices.get(key, position.entry_price)
            rules = self.rules.get(key, default_market_rules(self.config, key))
            bracket = _bracket(rules, position.quantity * mark_price)
            positions.append(
                PositionRiskSnapshot(
                    symbol=key,
                    side=position.side,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    mark_price=mark_price,
                    initial_risk=position.initial_risk,
                    maintenance_margin_rate=bracket.maintenance_margin_rate,
                    correlation_group=key,
                    stop_price=position.stop_price,
                )
            )
        payload = {
            "account_id": "event-engine",
            "observed_at": bar.timestamp,
            "equity": equity,
            "available_balance": max(0.0, equity - maintenance),
            "maintenance_margin": maintenance,
            "day_start_equity": self.state.day_start_equity or equity,
            "peak_equity": self.state.peak_equity or equity,
            "consecutive_losses": self.state.consecutive_losses,
            "positions": [position.to_dict() for position in positions],
            "global_halt": self.state.global_stop,
            "day_halt": self.state.day_paused,
            "drawdown_halt": self.state.drawdown_halted,
        }
        return AccountRiskSnapshot(
            snapshot_id=_stable_identifier("event_engine_account", payload),
            account_id="event-engine",
            observed_at=bar.timestamp,
            equity=equity,
            available_balance=max(0.0, equity - maintenance),
            maintenance_margin=maintenance,
            day_start_equity=self.state.day_start_equity or equity,
            peak_equity=self.state.peak_equity or equity,
            consecutive_losses=self.state.consecutive_losses,
            positions=tuple(positions),
            global_halt=self.state.global_stop,
            day_halt=self.state.day_paused,
            drawdown_halt=self.state.drawdown_halted,
        )

    def _entry_market_snapshot(self, symbol: str, bar: Candle) -> MarketRiskSnapshot:
        rules = self.rules.get(symbol, default_market_rules(self.config, symbol))
        payload = {
            "symbol": symbol,
            "observed_at": bar.timestamp,
            "mark_price": bar.open,
            "rules": {
                "tick_size": rules.tick_size,
                "step_size": rules.step_size,
                "min_quantity": rules.min_quantity,
                "min_notional": rules.min_notional,
                "max_leverage": rules.max_leverage,
                "brackets": [asdict(bracket) for bracket in rules.brackets],
            },
        }
        return MarketRiskSnapshot(
            snapshot_id=_stable_identifier("event_engine_market", payload),
            symbol=symbol,
            observed_at=bar.timestamp,
            mark_price=bar.open,
            rules=rules,
            correlation_group=symbol,
        )

    def _entry_risk_decision(
        self,
        symbol: str,
        signal: Signal,
        bar: Candle,
    ) -> RiskDecision:
        return self.risk_evaluator(
            RiskContext(
                "event-engine-risk-v1",
                self._entry_risk_intent(symbol, signal, bar),
                self._entry_account_snapshot(symbol, bar),
                self._entry_market_snapshot(symbol, bar),
                RiskPolicy.from_strategy_config(self.config),
                bar.timestamp,
            )
        )

    def _open(self, symbol: str, signal: Signal, bar: Candle) -> None:
        decision = self._entry_risk_decision(symbol, signal, bar)
        if decision.status is RiskDecisionStatus.REJECTED:
            self._record(
                bar.timestamp,
                "entry_rejected",
                symbol=symbol,
                reason=decision.reasons[0].value,
                reasons=[reason.value for reason in decision.reasons],
                risk_decision_id=decision.decision_id,
            )
            return

        side = signal.action.removeprefix("enter_")
        expected_action = (
            RiskApprovedAction.OPEN_LONG if side == "long" else RiskApprovedAction.OPEN_SHORT
        )
        if decision.approved_action is not expected_action:
            raise RuntimeError("risk evaluator returned the wrong approved entry action")
        if decision.stop_price is None:
            raise RuntimeError("approved entry has no stop price")

        rules = self.rules.get(symbol, default_market_rules(self.config, symbol))
        equity = self.equity({symbol: decision.approval_price})
        fee = decision.notional * self.config.fee_rate
        if fee >= self.state.cash:
            self._record(
                bar.timestamp,
                "entry_rejected",
                symbol=symbol,
                reason="post_approval_insufficient_cash",
                risk_decision_id=decision.decision_id,
            )
            return
        risk_per_unit = decision.initial_risk / decision.quantity
        target_distance = self.config.reward_to_risk * risk_per_unit
        target = (
            decision.approval_price + target_distance
            if side == "long"
            else decision.approval_price - target_distance
        )
        target = _round_price(target, rules.tick_size)
        bracket = _bracket(rules, decision.notional)
        effective = decision.notional / max(equity, 1e-12)

        self.state.cash -= fee
        self.state.positions[symbol] = Position(
            symbol,
            side,
            decision.quantity,
            decision.approval_price,
            decision.stop_price,
            target,
            bar.timestamp,
            fee,
            effective,
            decision.initial_risk,
            bracket.maintenance_margin_rate,
        )
        self.state.funding_accrued[symbol] = 0.0
        if effective > 10 and timeframe_ms(self.config.low_timeframe) > 60_000:
            self.state.warnings.add(
                "A trade exceeded 10x effective leverage on bars coarser than 1m; "
                "intrabar liquidation ordering is not reliable."
            )
        self._record(
            bar.timestamp,
            "position_opened",
            symbol=symbol,
            side=side,
            quantity=decision.quantity,
            leverage=effective,
            approved_leverage=decision.leverage,
            initial_risk=decision.initial_risk,
            risk_decision_id=decision.decision_id,
        )

    def _apply_funding(
        self, symbol: str, bar: Candle, funding_rates: list[FundingRate] | None
    ) -> None:
        position = self.state.positions.get(symbol)
        if position is None:
            return
        close_time = bar.timestamp + timeframe_ms(self.config.low_timeframe)
        applied = set(self.state.applied_funding.setdefault(symbol, []))
        if funding_rates:
            candidates = funding_rates
        else:
            next_time = self.state.next_fallback_funding.setdefault(
                symbol, ((position.entry_time // FUNDING_INTERVAL_MS) + 1) * FUNDING_INTERVAL_MS
            )
            candidates = []
            while next_time <= close_time:
                candidates.append(FundingRate(next_time, self.config.fallback_funding_rate_8h))
                next_time += FUNDING_INTERVAL_MS
            self.state.next_fallback_funding[symbol] = next_time
        for rate in candidates:
            if position.entry_time < rate.timestamp <= close_time and rate.timestamp not in applied:
                sign = 1 if position.side == "long" else -1
                payment = sign * position.quantity * bar.close * rate.rate
                self.state.funding_accrued[symbol] = (
                    self.state.funding_accrued.get(symbol, 0.0) + payment
                )
                applied.add(rate.timestamp)
                self._record(
                    rate.timestamp,
                    "funding",
                    symbol=symbol,
                    amount=payment,
                    source="exchange" if funding_rates else "fallback",
                )
        self.state.applied_funding[symbol] = sorted(applied)[-2000:]

    def _cross_liquidation(self, symbol: str, mark: Candle, timestamp: int) -> bool:
        position = self.state.positions.get(symbol)
        if position is None:
            return False
        adverse = mark.low if position.side == "long" else mark.high
        overrides = {symbol: adverse}
        liquidation_fees = sum(
            p.quantity
            * (adverse if key == symbol else self.state.latest_prices.get(key, p.entry_price))
            * self.config.liquidation_fee_rate
            for key, p in self.state.positions.items()
        )
        if self.equity(overrides) > self.maintenance_requirement(overrides) + liquidation_fees:
            return False
        for key in list(self.state.positions):
            price = (
                adverse
                if key == symbol
                else self.state.latest_prices.get(key, self.state.positions[key].entry_price)
            )
            self._close(key, price, timestamp, "liquidation")
        self.state.global_stop = True
        self.state.warnings.add(
            "Cross-margin liquidation occurred; all positions were closed and the engine halted."
        )
        return True

    def _risk_update(self, timestamp: int) -> None:
        equity = self.equity()
        self.state.equity_curve.append((timestamp, equity))
        day = timestamp // 86_400_000
        if day != self.state.day_id:
            self.state.day_id = day
            self.state.day_start_equity = equity
            self.state.day_paused = False
            self.state.consecutive_losses = 0
        self.state.peak_equity = max(self.state.peak_equity or equity, equity)
        if equity <= (self.state.peak_equity or equity) * (1 - self.config.max_account_drawdown):
            self.state.drawdown_halted = True
            self.state.warnings.add(
                "New entries halted after the maximum account drawdown was reached."
            )
        if equity <= (self.state.day_start_equity or equity) * (1 - self.config.daily_loss_limit):
            self.state.day_paused = True
            self.state.warnings.add("At least one trading day hit the daily loss limit.")
        if self.state.consecutive_losses >= self.config.max_consecutive_losses:
            self.state.day_paused = True
            self.state.warnings.add("At least one trading day hit the consecutive-loss limit.")

    def process_bar(
        self,
        symbol: str,
        bar: Candle,
        low_history: list[Candle],
        closed_high_history: list[Candle],
        funding_rates: list[FundingRate] | None = None,
        mark_bar: Candle | None = None,
        generate_signal: bool = True,
        strategy_config: StrategyConfig | None = None,
    ) -> None:
        if bar.timestamp <= self.state.last_timestamps.get(symbol, -1):
            return
        self.state.latest_prices[symbol] = (mark_bar or bar).close
        pending = self.state.pending.pop(symbol, None)
        if pending is not None:
            if pending.action == "exit" and symbol in self.state.positions:
                self._close(symbol, bar.open, bar.timestamp, pending.reason)
            elif pending.action.startswith("enter_") and symbol not in self.state.positions:
                self._open(symbol, pending, bar)

        if self._cross_liquidation(symbol, mark_bar or bar, bar.timestamp):
            self._risk_update(bar.timestamp + timeframe_ms(self.config.low_timeframe))
            self.state.last_timestamps[symbol] = bar.timestamp
            return

        position = self.state.positions.get(symbol)
        if position is not None:
            event: tuple[float, str] | None = None
            if position.side == "long":
                if bar.low <= position.stop_price:
                    event = (position.stop_price, "stop_loss")
                elif bar.high >= position.take_profit:
                    event = (position.take_profit, "take_profit")
            else:
                if bar.high >= position.stop_price:
                    event = (position.stop_price, "stop_loss")
                elif bar.low <= position.take_profit:
                    event = (position.take_profit, "take_profit")
            if event:
                self._close(symbol, event[0], bar.timestamp, event[1])

        decision_config = strategy_config or self.config
        self._apply_funding(symbol, bar, funding_rates)
        close_time = bar.timestamp + timeframe_ms(decision_config.low_timeframe)
        self._risk_update(close_time)
        if generate_signal and closed_high_history and not self.state.global_stop:
            current_side = self.state.positions.get(symbol)
            signal = self.decision_function(
                low_history,
                closed_high_history,
                decision_config,
                current_side.side if current_side else None,
            ).signal
            if signal is not None:
                self.state.pending[symbol] = signal
                self._record(
                    close_time, "signal", symbol=symbol, action=signal.action, reason=signal.reason
                )
        self.state.last_timestamps[symbol] = bar.timestamp

    def close_all(self, timestamp: int, reason: str = "end_of_test") -> None:
        for symbol in list(self.state.positions):
            self._close(symbol, self.state.latest_prices[symbol], timestamp, reason)
        if self.state.equity_curve:
            self.state.equity_curve[-1] = (self.state.equity_curve[-1][0], self.state.cash)


def max_drawdown(curve: list[tuple[int, float]]) -> float:
    if not curve:
        return 0.0
    peak = curve[0][1]
    result = 0.0
    for _, value in curve:
        peak = max(peak, value)
        result = max(result, (peak - value) / peak if peak > 0 else 1.0)
    return min(1.0, max(0.0, result))


def safe_profit_factor(trades: list[Trade]) -> float | None:
    wins = sum(trade.net_pnl for trade in trades if trade.net_pnl > 0)
    losses = -sum(trade.net_pnl for trade in trades if trade.net_pnl < 0)
    if losses:
        return wins / losses
    return None if wins else 0.0
