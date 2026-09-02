from __future__ import annotations

from decimal import Decimal

from .config import StrategyConfig
from .data import timeframe_ms
from .domain import Candle, FundingRate, MarketRules, Signal
from .engine import (
    FUNDING_INTERVAL_MS,
    EngineState,
    EventEngine,
    Position,
    Trade,
    _bracket,
    _round_price,
    _stable_identifier,
    default_market_rules,
)
from .execution_contracts import ExecutionMode, ExecutionPolicy
from .portfolio_facts import make_mark_price_fact
from .research_accounting import ResearchAccount
from .research_execution import ResearchExecutionFactory
from .research_trading import ResearchTradeCoordinator, ResearchTradeError
from .risk_contracts import RiskDecisionStatus, RiskPolicy
from .strategy import strategy_spec
from .strategy_contracts import StrategyAction, StrategyIntent, strategy_parameter_hash


class ResearchEventEngine(EventEngine):
    """Fresh-state EventEngine whose economic facts come from Portfolio accounting."""

    def __init__(
        self,
        config: StrategyConfig,
        state: EngineState | None = None,
        market_rules: dict[str, MarketRules] | None = None,
        decision_function=None,
    ) -> None:
        if state is not None:
            untouched = (
                state.cash == config.initial_cash
                and not state.positions
                and not state.pending
                and not state.trades
                and not state.events
                and not state.equity_curve
            )
            if not untouched:
                raise ValueError(
                    "ResearchEventEngine accepts fresh state only; legacy EngineState "
                    "positions and cash cannot be inferred into an authoritative ledger"
                )
        fresh = EngineState(config.initial_cash)
        super().__init__(
            config,
            state=fresh,
            market_rules=market_rules,
            decision_function=decision_function,
        )
        self.account = ResearchAccount.initialized(
            account_id="research-event-engine",
            initial_capital=config.initial_cash,
            event_time=0,
            source_id="research-event-engine-capital",
        )
        self.planner = ResearchExecutionFactory(
            config,
            risk_policy=RiskPolicy.from_strategy_config(config),
            execution_policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
            risk_instance_id="research-event-risk-v1",
            execution_instance_id="research-event-execution-v1",
        )
        self.trading = ResearchTradeCoordinator(
            account=self.account,
            planner=self.planner,
            rules=self.rules,
            fee_rate=config.fee_rate,
            maintenance_margin_rate=config.maintenance_margin_rate,
            maintenance_rate_provider=self._maintenance_rate,
        )
        self._targets: dict[str, float] = {}
        self._effective_leverage: dict[str, float] = {}
        self._entry_times: dict[str, int] = {}
        self._mark_sequence = 0

    def _rules_for(self, symbol: str) -> MarketRules:
        rules = self.rules.get(symbol)
        if rules is None:
            rules = default_market_rules(self.config, symbol)
            self.rules[symbol] = rules
            self.trading.rules[symbol] = rules
        return rules

    def _maintenance_rate(self, symbol: str, notional: Decimal) -> Decimal:
        rules = self._rules_for(symbol)
        return Decimal(str(_bracket(rules, float(notional)).maintenance_margin_rate))

    def _sync_cash(self) -> None:
        self.state.cash = float(self.account.balances.cash)

    def _sync_position(self, symbol: str) -> None:
        current = self.account.positions.get(symbol)
        if current is None:
            self.state.positions.pop(symbol, None)
            self.state.funding_accrued.pop(symbol, None)
            return
        self.state.positions[symbol] = Position(
            symbol=symbol,
            side=current.side.value,
            quantity=float(current.quantity),
            entry_price=float(current.average_entry_price),
            stop_price=float(current.stop_price),
            take_profit=self._targets[symbol],
            entry_time=self._entry_times[symbol],
            entry_fee=float(current.opening_fees),
            effective_leverage=self._effective_leverage[symbol],
            initial_risk=float(current.initial_risk),
            maintenance_margin_rate=float(current.maintenance_margin_rate),
        )
        self.state.funding_accrued[symbol] = -float(current.funding_pnl)

    def equity(self, overrides: dict[str, float] | None = None) -> float:
        prices = overrides or {}
        value = self.account.balances.settled_assets
        for symbol, position in self.account.positions.items():
            price = Decimal(str(prices.get(symbol, float(position.mark_price))))
            sign = Decimal("1") if position.side.value == "long" else Decimal("-1")
            value += sign * position.quantity * (price - position.average_entry_price)
        return float(value)

    def maintenance_requirement(self, overrides: dict[str, float] | None = None) -> float:
        prices = overrides or {}
        total = Decimal("0")
        for symbol, position in self.account.positions.items():
            price = Decimal(str(prices.get(symbol, float(position.mark_price))))
            rules = self._rules_for(symbol)
            notional = position.quantity * price
            bracket = _bracket(rules, float(notional))
            total += max(
                Decimal("0"),
                notional * Decimal(str(bracket.maintenance_margin_rate))
                - Decimal(str(bracket.maintenance_amount)),
            )
        return float(total)

    def _mark(self, symbol: str, price: float, timestamp: int) -> None:
        if timestamp < 0 or price <= 0:
            raise ValueError("invalid research mark fact")
        self._mark_sequence += 1
        fact = make_mark_price_fact(
            account_id=self.account.account_id,
            source_event_id=_stable_identifier(
                "research_event_mark",
                {
                    "symbol": symbol,
                    "timestamp": timestamp,
                    "price": price,
                    "source_sequence": self._mark_sequence,
                },
            ),
            symbol=symbol,
            observed_at=timestamp,
            source_sequence=self._mark_sequence,
            mark_price=price,
        )
        self.account.apply_mark(fact)
        if symbol in self.account.positions:
            self._sync_position(symbol)

    def _exit_intent(
        self,
        symbol: str,
        reference_price: float,
        timestamp: int,
        reason: str,
    ) -> StrategyIntent:
        spec = strategy_spec(self.config.entry_model)
        source_payload = {
            "symbol": symbol,
            "decision_time": timestamp,
            "action": StrategyAction.EXIT.value,
            "reference_price": reference_price,
            "reason": reason,
        }
        source_event_id = _stable_identifier("research_event_exit", source_payload)
        intent_payload = {
            **source_payload,
            "source_event_id": source_event_id,
            "parameter_hash": strategy_parameter_hash(self.config),
        }
        return StrategyIntent(
            intent_id=_stable_identifier("research_event_exit_intent", intent_payload),
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            instance_id=f"research-event:{symbol}",
            symbol=symbol,
            action=StrategyAction.EXIT,
            decision_time=timestamp,
            created_at=timestamp,
            reference_price=reference_price,
            stop_price=None,
            reason=reason,
            source_event_id=source_event_id,
            source_sequence=0,
            high_timeframe=self.config.high_timeframe,
            low_timeframe=self.config.low_timeframe,
            parameter_hash=strategy_parameter_hash(self.config),
            evidence={"adapter": "ResearchEventEngine"},
        )

    def _open(self, symbol: str, signal: Signal, bar: Candle) -> None:
        try:
            intent = self._entry_risk_intent(symbol, signal, bar)
        except ValueError as exc:
            self._record(
                bar.timestamp,
                "entry_rejected",
                symbol=symbol,
                reason="invalid_strategy_intent",
                error=str(exc),
            )
            return
        self._rules_for(symbol)
        equity_before = self.equity({symbol: bar.open})
        try:
            result = self.trading.open_full(
                intent=intent,
                risk_mark_price=bar.open,
                execution_price=bar.open,
                event_time=bar.timestamp,
                day_start_equity=self.state.day_start_equity,
                peak_equity=self.state.peak_equity,
                consecutive_losses=self.state.consecutive_losses,
                global_halt=self.state.global_stop,
                day_halt=self.state.day_paused,
                drawdown_halt=self.state.drawdown_halted,
            )
        except (ResearchTradeError, ValueError) as exc:
            self._record(
                bar.timestamp,
                "entry_rejected",
                symbol=symbol,
                reason="portfolio_accounting_rejection",
                error=str(exc),
            )
            return
        decision = result.plan.risk_decision
        if result.disposition != "filled" or result.position is None:
            reason = (
                decision.reasons[0].value
                if decision.status is RiskDecisionStatus.REJECTED
                else result.disposition
            )
            self._record(
                bar.timestamp,
                "entry_rejected",
                symbol=symbol,
                reason=reason,
                reasons=[item.value for item in decision.reasons],
                risk_decision_id=decision.decision_id,
            )
            return
        position = result.position
        risk_per_unit = decision.initial_risk / decision.quantity
        target_distance = self.config.reward_to_risk * risk_per_unit
        target = (
            decision.approval_price + target_distance
            if position.side.value == "long"
            else decision.approval_price - target_distance
        )
        target = _round_price(target, self._rules_for(symbol).tick_size)
        effective = decision.notional / max(equity_before, 1e-12)
        self._targets[symbol] = target
        self._effective_leverage[symbol] = effective
        self._entry_times[symbol] = bar.timestamp
        self._sync_cash()
        self._sync_position(symbol)
        if effective > 10 and timeframe_ms(self.config.low_timeframe) > 60_000:
            self.state.warnings.add(
                "A trade exceeded 10x effective leverage on bars coarser than 1m; "
                "intrabar liquidation ordering is not reliable."
            )
        self._record(
            bar.timestamp,
            "position_opened",
            symbol=symbol,
            side=position.side.value,
            quantity=float(position.quantity),
            leverage=effective,
            approved_leverage=decision.leverage,
            initial_risk=decision.initial_risk,
            risk_decision_id=decision.decision_id,
            execution_command_id=result.plan.command.command_id if result.plan.command else None,
        )

    def _close(self, symbol: str, raw_price: float, timestamp: int, reason: str) -> None:
        position = self.state.positions[symbol]
        slip = (
            1 - self.config.slippage_rate
            if position.side == "long"
            else 1 + self.config.slippage_rate
        )
        price = raw_price * slip
        exit_intent = None
        if reason != "stop_loss":
            exit_intent = self._exit_intent(symbol, raw_price, timestamp, reason)
        fee_rate = (
            self.config.liquidation_fee_rate if reason == "liquidation" else self.config.fee_rate
        )
        result = self.trading.close_full(
            symbol=symbol,
            execution_price=price,
            risk_mark_price=raw_price,
            event_time=timestamp,
            exit_intent=exit_intent,
            fee_rate=fee_rate,
            reason=reason,
            day_start_equity=self.state.day_start_equity,
            peak_equity=self.state.peak_equity,
            consecutive_losses=self.state.consecutive_losses,
            global_halt=self.state.global_stop,
            day_halt=self.state.day_paused,
            drawdown_halt=self.state.drawdown_halted,
        )
        if result.disposition != "filled" or result.position is not None:
            raise ResearchTradeError(
                f"research close did not flatten {symbol}: {result.disposition}"
            )
        closed = self.account.closed_positions[-1]
        net = float(closed.net_pnl)
        trade = Trade(
            symbol=closed.symbol,
            side=closed.side.value,
            entry_time=closed.opened_at,
            exit_time=closed.closed_at,
            entry_price=float(closed.average_entry_price),
            exit_price=float(closed.average_exit_price),
            quantity=float(closed.total_quantity),
            gross_pnl=float(closed.realized_pnl),
            fees=float(closed.opening_fees + closed.closing_fees),
            funding=-float(closed.funding_pnl),
            net_pnl=net,
            exit_reason=reason,
            effective_leverage=self._effective_leverage[symbol],
        )
        self.state.trades.append(trade)
        self.state.consecutive_losses = self.state.consecutive_losses + 1 if net < 0 else 0
        self._sync_cash()
        self._sync_position(symbol)
        self._targets.pop(symbol, None)
        self._effective_leverage.pop(symbol, None)
        self._entry_times.pop(symbol, None)
        self._record(timestamp, "position_closed", symbol=symbol, reason=reason, net_pnl=net)

    def _apply_funding(
        self,
        symbol: str,
        bar: Candle,
        funding_rates: list[FundingRate] | None,
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
                symbol,
                ((position.entry_time // FUNDING_INTERVAL_MS) + 1) * FUNDING_INTERVAL_MS,
            )
            candidates = []
            while next_time <= close_time:
                candidates.append(FundingRate(next_time, self.config.fallback_funding_rate_8h))
                next_time += FUNDING_INTERVAL_MS
            self.state.next_fallback_funding[symbol] = next_time
        for rate in candidates:
            if position.entry_time < rate.timestamp <= close_time and rate.timestamp not in applied:
                source = "exchange" if funding_rates else "fallback"
                updated = self.trading.apply_funding(
                    symbol=symbol,
                    rate=rate.rate,
                    mark_price=bar.close,
                    event_time=rate.timestamp,
                    source_id=_stable_identifier(
                        "research_event_funding",
                        {
                            "symbol": symbol,
                            "timestamp": rate.timestamp,
                            "rate": rate.rate,
                            "mark_price": bar.close,
                            "source": source,
                        },
                    ),
                )
                payment = -float(updated.funding_pnl) - self.state.funding_accrued.get(
                    symbol,
                    0.0,
                )
                self._sync_cash()
                self._sync_position(symbol)
                applied.add(rate.timestamp)
                self._record(
                    rate.timestamp,
                    "funding",
                    symbol=symbol,
                    amount=payment,
                    source=source,
                )
        self.state.applied_funding[symbol] = sorted(applied)[-2000:]

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
        self._rules_for(symbol)
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
        if symbol in self.account.positions:
            self._mark(symbol, (mark_bar or bar).close, close_time)
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
                    close_time,
                    "signal",
                    symbol=symbol,
                    action=signal.action,
                    reason=signal.reason,
                )
        self.state.last_timestamps[symbol] = bar.timestamp
