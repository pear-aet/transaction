from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .domain import MarketRules
from .execution_contracts import (
    ExecutionCommandStatus,
    ExecutionOrder,
    OrderLegType,
    OrderUpdate,
)
from .portfolio_contracts import PortfolioPosition, as_decimal
from .research_accounting import ResearchAccount
from .research_execution import ResearchExecutionFactory, ResearchExecutionPlan
from .strategy_contracts import StrategyIntent


class ResearchTradeError(RuntimeError):
    """Raised when a research trade lifecycle cannot advance safely."""


@dataclass(frozen=True, slots=True)
class ResearchTradeResult:
    plan: ResearchExecutionPlan
    order: ExecutionOrder | None
    update: OrderUpdate | None
    position: PortfolioPosition | None
    disposition: str


@dataclass(slots=True)
class ResearchTradeCoordinator:
    """Coordinate full research trade lifecycles on production contracts."""

    account: ResearchAccount
    planner: ResearchExecutionFactory
    rules: dict[str, MarketRules]
    fee_rate: Decimal | int | float | str
    maintenance_margin_rate: Decimal | int | float | str
    correlation_groups: dict[str, str] = field(default_factory=dict)
    entry_plans: dict[str, ResearchExecutionPlan] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.fee_rate = as_decimal(self.fee_rate, "fee_rate")
        self.maintenance_margin_rate = as_decimal(
            self.maintenance_margin_rate,
            "maintenance_margin_rate",
        )
        if not Decimal("0") <= self.fee_rate <= Decimal("0.1"):
            raise ValueError("research fee_rate must be in [0, 0.1]")
        if not Decimal("0") < self.maintenance_margin_rate < Decimal("1"):
            raise ValueError("invalid research maintenance margin rate")
        if not self.rules:
            raise ValueError("research coordinator requires market rules")

    def _rules(self, symbol: str) -> MarketRules:
        try:
            return self.rules[symbol]
        except KeyError as exc:
            raise ResearchTradeError(f"research market rules are missing: {symbol}") from exc

    @staticmethod
    def _primary(plan: ResearchExecutionPlan) -> ExecutionOrder:
        if plan.command is None:
            raise ResearchTradeError("research plan has no execution command")
        orders = [
            order for order in plan.command.orders if order.leg_type is OrderLegType.PRIMARY
        ]
        if len(orders) != 1:
            raise ResearchTradeError("research command requires one primary order")
        return orders[0]

    @staticmethod
    def _protective(plan: ResearchExecutionPlan) -> ExecutionOrder:
        if plan.command is None:
            raise ResearchTradeError("research plan has no execution command")
        orders = [
            order
            for order in plan.command.orders
            if order.leg_type is OrderLegType.PROTECTIVE_STOP
        ]
        if len(orders) != 1:
            raise ResearchTradeError("research entry requires one protective order")
        return orders[0]

    def _fill_update(
        self,
        *,
        order: ExecutionOrder,
        quantity: Decimal,
        price: Decimal,
        event_time: int,
        fee_rate: Decimal,
        evidence: dict[str, object],
    ) -> OrderUpdate:
        quote = quantity * price
        fee = self.account.policy.quantize_money(quote * fee_rate)
        return self.planner.cumulative_fill_update(
            order=order,
            event_time=event_time,
            cumulative_quantity=quantity,
            cumulative_quote_value=quote,
            cumulative_fee=fee,
            evidence=evidence,
        )

    def open_full(
        self,
        *,
        intent: StrategyIntent,
        risk_mark_price: Decimal | int | float | str,
        execution_price: Decimal | int | float | str,
        event_time: int,
        day_start_equity: Decimal | int | float | str | None = None,
        peak_equity: Decimal | int | float | str | None = None,
        consecutive_losses: int = 0,
        global_halt: bool = False,
        day_halt: bool = False,
        drawdown_halt: bool = False,
    ) -> ResearchTradeResult:
        if intent.symbol in self.account.positions:
            raise ResearchTradeError(f"research position already exists: {intent.symbol}")
        selected_execution = as_decimal(execution_price, "execution_price")
        plan = self.planner.plan(
            account=self.account,
            intent=intent,
            rules=self._rules(intent.symbol),
            risk_mark_price=risk_mark_price,
            execution_price=selected_execution,
            planned_at=event_time,
            correlation_group=self.correlation_groups.get(intent.symbol, "default"),
            day_start_equity=day_start_equity,
            peak_equity=peak_equity,
            consecutive_losses=consecutive_losses,
            global_halt=global_halt,
            day_halt=day_halt,
            drawdown_halt=drawdown_halt,
        )
        if plan.command is None:
            return ResearchTradeResult(plan, None, None, None, "risk_rejected")
        if plan.command.status is not ExecutionCommandStatus.DISPATCH_PENDING:
            return ResearchTradeResult(plan, None, None, None, "execution_not_ready")
        reservation = self.account.reserve(
            command=plan.command,
            decision=plan.risk_decision,
            now_ms=event_time,
        )
        if not reservation.entry:
            raise ResearchTradeError("entry plan created a non-entry reservation")
        order = self._primary(plan)
        quantity = as_decimal(order.quantity, "entry quantity")
        update = self._fill_update(
            order=order,
            quantity=quantity,
            price=selected_execution,
            event_time=event_time,
            fee_rate=self.fee_rate,
            evidence={"research_disposition": "entry_fill"},
        )
        position = self.account.apply_open_update(
            command=plan.command,
            decision=plan.risk_decision,
            order=order,
            update=update,
            maintenance_margin_rate=self.maintenance_margin_rate,
            correlation_group=self.correlation_groups.get(intent.symbol, "default"),
        )
        self.entry_plans[intent.symbol] = plan
        return ResearchTradeResult(plan, order, update, position, "filled")

    def close_full(
        self,
        *,
        symbol: str,
        execution_price: Decimal | int | float | str,
        event_time: int,
        exit_intent: StrategyIntent | None = None,
        risk_mark_price: Decimal | int | float | str | None = None,
        fee_rate: Decimal | int | float | str | None = None,
        reason: str,
        day_start_equity: Decimal | int | float | str | None = None,
        peak_equity: Decimal | int | float | str | None = None,
        consecutive_losses: int = 0,
        global_halt: bool = False,
        day_halt: bool = False,
        drawdown_halt: bool = False,
    ) -> ResearchTradeResult:
        position = self.account.positions.get(symbol)
        if position is None:
            raise ResearchTradeError(f"research position is missing: {symbol}")
        selected_execution = as_decimal(execution_price, "execution_price")
        selected_fee = (
            self.fee_rate if fee_rate is None else as_decimal(fee_rate, "close fee_rate")
        )
        if exit_intent is None:
            try:
                plan = self.entry_plans[symbol]
            except KeyError as exc:
                raise ResearchTradeError(
                    f"research protective plan is missing: {symbol}"
                ) from exc
            order = self._protective(plan)
        else:
            if exit_intent.symbol != symbol:
                raise ResearchTradeError("exit intent targets another research symbol")
            selected_mark = (
                selected_execution
                if risk_mark_price is None
                else as_decimal(risk_mark_price, "risk_mark_price")
            )
            plan = self.planner.plan(
                account=self.account,
                intent=exit_intent,
                rules=self._rules(symbol),
                risk_mark_price=selected_mark,
                execution_price=selected_execution,
                planned_at=event_time,
                correlation_group=self.correlation_groups.get(symbol, "default"),
                day_start_equity=day_start_equity,
                peak_equity=peak_equity,
                consecutive_losses=consecutive_losses,
                global_halt=global_halt,
                day_halt=day_halt,
                drawdown_halt=drawdown_halt,
            )
            if plan.command is None:
                return ResearchTradeResult(plan, None, None, position, "risk_rejected")
            if plan.command.status is not ExecutionCommandStatus.DISPATCH_PENDING:
                return ResearchTradeResult(
                    plan,
                    None,
                    None,
                    position,
                    "execution_not_ready",
                )
            reservation = self.account.reserve(
                command=plan.command,
                decision=plan.risk_decision,
                now_ms=event_time,
            )
            if reservation.entry:
                raise ResearchTradeError("exit plan created an entry reservation")
            order = self._primary(plan)
        quantity = position.quantity
        update = self._fill_update(
            order=order,
            quantity=quantity,
            price=selected_execution,
            event_time=event_time,
            fee_rate=selected_fee,
            evidence={"research_disposition": "close_fill", "reason": reason},
        )
        remaining = self.account.apply_close_update(
            command=plan.command,
            order=order,
            update=update,
        )
        self.account.release(
            command_id=plan.command.command_id,
            event_time=event_time,
            reason=reason,
        )
        entry_plan = self.entry_plans.pop(symbol, None)
        if entry_plan is not None and entry_plan.command is not None:
            self.account.release(
                command_id=entry_plan.command.command_id,
                event_time=event_time,
                reason=f"position_closed:{reason}",
            )
        return ResearchTradeResult(plan, order, update, remaining, "filled")

    def apply_funding(
        self,
        *,
        symbol: str,
        rate: Decimal | int | float | str,
        mark_price: Decimal | int | float | str,
        event_time: int,
        source_id: str,
    ) -> PortfolioPosition:
        return self.account.apply_funding(
            symbol=symbol,
            rate=rate,
            mark_price=mark_price,
            event_time=event_time,
            source_id=source_id,
        )
