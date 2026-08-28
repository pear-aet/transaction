from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/crypto_quant/engine.py",
    '''from __future__ import annotations

from collections.abc import Callable
''',
    '''from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
''',
)
replace_once(
    "src/crypto_quant/engine.py",
    '''from .domain import Candle, FundingRate, MarketRules, RiskBracket, Signal, finite_json
from .strategy import decide
''',
    '''from .domain import Candle, FundingRate, MarketRules, RiskBracket, Signal, finite_json
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
''',
)
replace_once(
    "src/crypto_quant/engine.py",
    '''FUNDING_INTERVAL_MS = 28_800_000


@dataclass(slots=True)
''',
    '''FUNDING_INTERVAL_MS = 28_800_000


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
''',
)
replace_once(
    "src/crypto_quant/engine.py",
    '''        market_rules: dict[str, MarketRules] | None = None,
        decision_function: Callable | None = None,
    ) -> None:
''',
    '''        market_rules: dict[str, MarketRules] | None = None,
        decision_function: Callable | None = None,
        risk_evaluator: Callable[[RiskContext], RiskDecision] | None = None,
    ) -> None:
''',
)
replace_once(
    "src/crypto_quant/engine.py",
    '''        self.rules = market_rules or {config.symbol: default_market_rules(config)}
        self.decision_function = decision_function or decide
        if self.state.peak_equity is None:
''',
    '''        self.rules = market_rules or {config.symbol: default_market_rules(config)}
        self.decision_function = decision_function or decide
        self.risk_evaluator = risk_evaluator or evaluate_risk
        if self.state.peak_equity is None:
''',
)

old_open = '''    def _open(self, symbol: str, signal: Signal, bar: Candle) -> None:
        if self.state.global_stop or self.state.day_paused or self.state.drawdown_halted:
            self._record(bar.timestamp, "entry_rejected", symbol=symbol, reason="risk_halt")
            return
        rules = self.rules.get(symbol, default_market_rules(self.config, symbol))
        side = signal.action.removeprefix("enter_")
        fill = bar.open * (
            1 + self.config.slippage_rate if side == "long" else 1 - self.config.slippage_rate
        )
        stop = float(signal.stop_price or 0.0)
        price_risk = fill - stop if side == "long" else stop - fill
        if price_risk <= 0:
            self._record(bar.timestamp, "entry_rejected", symbol=symbol, reason="invalid_stop")
            return
        equity = self.equity({symbol: fill})
        open_risk = sum(position.initial_risk for position in self.state.positions.values())
        risk_budget = min(
            equity * self.config.risk_per_trade,
            equity * self.config.max_symbol_risk,
            max(0.0, equity * self.config.max_portfolio_risk - open_risk),
        )
        costs = fill * (2 * self.config.fee_rate + self.config.slippage_rate)
        risk_per_unit = price_risk + costs
        same_direction = sum(
            p.quantity * self.state.latest_prices.get(key, p.entry_price)
            for key, p in self.state.positions.items()
            if p.side == side
        )
        directional_room = max(0.0, equity * self.config.max_directional_notional - same_direction)
        leverage_cap = min(self.config.max_leverage, rules.max_leverage)
        bracket = rules.brackets[0]
        buffer_cap = 1 / (
            bracket.maintenance_margin_rate
            + self.config.liquidation_fee_rate
            + self.config.min_liquidation_buffer
        )
        leverage_cap = min(leverage_cap, buffer_cap)
        quantity = min(
            risk_budget / risk_per_unit if risk_per_unit > 0 else 0.0,
            equity * leverage_cap / fill,
            directional_room / fill,
        )
        quantity = _floor(quantity, rules.step_size)
        notional = quantity * fill
        if quantity < rules.min_quantity or notional < rules.min_notional or quantity <= 0:
            self._record(
                bar.timestamp, "entry_rejected", symbol=symbol, reason="market_minimum_or_risk"
            )
            return
        bracket = _bracket(rules, notional)
        fee = notional * self.config.fee_rate
        if fee >= self.state.cash:
            self._record(bar.timestamp, "entry_rejected", symbol=symbol, reason="insufficient_cash")
            return
        self.state.cash -= fee
        effective = notional / max(equity, 1e-12)
        target_distance = self.config.reward_to_risk * risk_per_unit
        target = fill + target_distance if side == "long" else fill - target_distance
        stop = _round_price(stop, rules.tick_size)
        target = _round_price(target, rules.tick_size)
        self.state.positions[symbol] = Position(
            symbol,
            side,
            quantity,
            fill,
            stop,
            target,
            bar.timestamp,
            fee,
            effective,
            quantity * risk_per_unit,
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
            quantity=quantity,
            leverage=effective,
        )

'''

new_open = '''    def _entry_risk_intent(
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
            RiskApprovedAction.OPEN_LONG
            if side == "long"
            else RiskApprovedAction.OPEN_SHORT
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

'''

replace_once("src/crypto_quant/engine.py", old_open, new_open)
