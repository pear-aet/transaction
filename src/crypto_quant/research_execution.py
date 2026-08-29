from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import Any

from .config import StrategyConfig
from .domain import MarketRules, finite_json
from .execution import build_execution_command
from .execution_contracts import (
    ExecutionCommand,
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionOrder,
    ExecutionPolicy,
    OrderState,
    OrderUpdate,
)
from .portfolio_accounting import build_account_snapshot
from .portfolio_contracts import AccountWatermarks, as_decimal
from .research_accounting import ResearchAccount
from .risk import evaluate_risk, validate_risk_decision
from .risk_contracts import (
    MarketRiskSnapshot,
    RiskContext,
    RiskDecision,
    RiskDecisionStatus,
    RiskPolicy,
)
from .strategy_contracts import StrategyIntent


class ResearchPlanningConflict(RuntimeError):
    """Raised when one research identity is replayed with different semantics."""


@dataclass(frozen=True, slots=True)
class ResearchExecutionPlan:
    intent: StrategyIntent
    risk_decision: RiskDecision
    execution_market: ExecutionMarketSnapshot | None
    command: ExecutionCommand | None
    planning_fingerprint: str

    @property
    def approved(self) -> bool:
        return self.risk_decision.status is RiskDecisionStatus.APPROVED


@dataclass(slots=True)
class ResearchExecutionFactory:
    """Pure-contract planning chain for deterministic backtests and research.

    The factory does not submit, amend or cancel orders. It materializes in-memory
    StrategyIntent and RiskDecision storage identities, calls the production Risk
    Engine, and delegates command construction to the production Execution planner.
    """

    strategy_config: StrategyConfig
    risk_policy: RiskPolicy | None = None
    execution_policy: ExecutionPolicy = field(
        default_factory=lambda: ExecutionPolicy(mode=ExecutionMode.SIMULATION)
    )
    risk_instance_id: str = "research-risk"
    execution_instance_id: str = "research-execution"
    _intent_semantics: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _intent_sequences: dict[str, int] = field(default_factory=dict, repr=False)
    _decision_semantics: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _decision_sequences: dict[str, int] = field(default_factory=dict, repr=False)
    _plans: dict[str, ResearchExecutionPlan] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.risk_instance_id or not self.execution_instance_id:
            raise ValueError("research planning instance identifiers must not be empty")
        if self.execution_policy.mode is not ExecutionMode.SIMULATION:
            raise ValueError("research execution must use local simulation mode")
        if self.risk_policy is None:
            self.risk_policy = RiskPolicy.from_strategy_config(self.strategy_config)

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            finite_json(payload),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _rules_payload(rules: MarketRules) -> dict[str, Any]:
        return {
            "symbol": rules.symbol,
            "tick_size": rules.tick_size,
            "step_size": rules.step_size,
            "min_quantity": rules.min_quantity,
            "min_notional": rules.min_notional,
            "max_leverage": rules.max_leverage,
            "brackets": [asdict(bracket) for bracket in rules.brackets],
        }

    def _persist_intent(self, intent: StrategyIntent) -> StrategyIntent:
        semantics = intent.semantic_dict()
        existing = self._intent_semantics.get(intent.intent_id)
        if existing is not None:
            if existing != semantics:
                raise ResearchPlanningConflict(
                    f"research intent identity collision: {intent.intent_id}"
                )
            sequence = self._intent_sequences[intent.intent_id]
            if intent.storage_sequence not in {None, sequence}:
                raise ResearchPlanningConflict(
                    f"research intent storage identity changed: {intent.intent_id}"
                )
            return replace(intent, storage_sequence=sequence)
        sequence = intent.storage_sequence or (len(self._intent_sequences) + 1)
        if sequence in self._intent_sequences.values():
            raise ResearchPlanningConflict(
                f"research intent storage sequence collision: {sequence}"
            )
        self._intent_semantics[intent.intent_id] = semantics
        self._intent_sequences[intent.intent_id] = sequence
        return replace(intent, storage_sequence=sequence)

    def _persist_decision(self, decision: RiskDecision) -> RiskDecision:
        semantics = decision.semantic_dict()
        existing = self._decision_semantics.get(decision.decision_id)
        if existing is not None:
            if existing != semantics:
                raise ResearchPlanningConflict(
                    f"research decision identity collision: {decision.decision_id}"
                )
            sequence = self._decision_sequences[decision.decision_id]
            if decision.storage_sequence not in {None, sequence}:
                raise ResearchPlanningConflict(
                    f"research decision storage identity changed: {decision.decision_id}"
                )
            return replace(decision, storage_sequence=sequence)
        sequence = decision.storage_sequence or (len(self._decision_sequences) + 1)
        if sequence in self._decision_sequences.values():
            raise ResearchPlanningConflict(
                f"research decision storage sequence collision: {sequence}"
            )
        self._decision_semantics[decision.decision_id] = semantics
        self._decision_sequences[decision.decision_id] = sequence
        return replace(decision, storage_sequence=sequence)

    def _account_snapshot(
        self,
        *,
        account: ResearchAccount,
        observed_at: int,
        day_start_equity: Decimal | int | float | str | None,
        peak_equity: Decimal | int | float | str | None,
        consecutive_losses: int,
        global_halt: bool,
        day_halt: bool,
        drawdown_halt: bool,
    ):
        equity = account.equity
        day_start = equity if day_start_equity is None else as_decimal(
            day_start_equity,
            "day_start_equity",
        )
        peak = max(
            equity,
            equity if peak_equity is None else as_decimal(peak_equity, "peak_equity"),
        )
        snapshot = build_account_snapshot(
            account_id=account.account_id,
            observed_at=observed_at,
            balances=account.balances,
            positions=tuple(account.positions.values()),
            day_start_equity=day_start,
            peak_equity=peak,
            consecutive_losses=consecutive_losses,
            watermarks=AccountWatermarks(),
            global_halt=global_halt,
            day_halt=day_halt,
            drawdown_halt=drawdown_halt,
        )
        return snapshot.to_risk_snapshot()

    def plan(
        self,
        *,
        account: ResearchAccount,
        intent: StrategyIntent,
        rules: MarketRules,
        risk_mark_price: Decimal | int | float | str,
        planned_at: int,
        execution_price: Decimal | int | float | str | None = None,
        correlation_group: str = "default",
        volatility_annualized: float | None = None,
        day_start_equity: Decimal | int | float | str | None = None,
        peak_equity: Decimal | int | float | str | None = None,
        consecutive_losses: int = 0,
        global_halt: bool = False,
        day_halt: bool = False,
        drawdown_halt: bool = False,
    ) -> ResearchExecutionPlan:
        if planned_at < 0 or consecutive_losses < 0:
            raise ValueError("research planning counters must be non-negative")
        if account.account_id == "":
            raise ValueError("research account identity must not be empty")
        if rules.symbol != intent.symbol:
            raise ValueError("research intent and market rules symbols differ")
        risk_mark = as_decimal(risk_mark_price, "risk_mark_price")
        selected_execution = (
            risk_mark
            if execution_price is None
            else as_decimal(execution_price, "execution_price")
        )
        if min(risk_mark, selected_execution) <= 0:
            raise ValueError("research market prices must be positive")
        persisted_intent = self._persist_intent(intent)
        account_snapshot = self._account_snapshot(
            account=account,
            observed_at=planned_at,
            day_start_equity=day_start_equity,
            peak_equity=peak_equity,
            consecutive_losses=consecutive_losses,
            global_halt=global_halt,
            day_halt=day_halt,
            drawdown_halt=drawdown_halt,
        )
        market_payload = {
            "symbol": intent.symbol,
            "observed_at": planned_at,
            "mark_price": str(risk_mark),
            "rules": self._rules_payload(rules),
            "correlation_group": correlation_group,
            "volatility_annualized": volatility_annualized,
        }
        risk_market = MarketRiskSnapshot(
            snapshot_id="research_risk_market:" + self._fingerprint(market_payload),
            symbol=intent.symbol,
            observed_at=planned_at,
            mark_price=float(risk_mark),
            rules=rules,
            correlation_group=correlation_group,
            volatility_annualized=volatility_annualized,
        )
        assert self.risk_policy is not None
        risk_context = RiskContext(
            self.risk_instance_id,
            persisted_intent,
            account_snapshot,
            risk_market,
            self.risk_policy,
            planned_at,
        )
        decision = self._persist_decision(
            validate_risk_decision(risk_context, evaluate_risk(risk_context))
        )
        planning_payload = {
            "intent": persisted_intent.to_dict(),
            "account_snapshot": account_snapshot.to_dict(),
            "risk_market": risk_market.to_dict(),
            "decision": decision.to_dict(),
            "execution_price": str(selected_execution),
            "execution_policy": self.execution_policy.to_dict(),
            "planned_at": planned_at,
        }
        fingerprint = self._fingerprint(planning_payload)
        existing_plan = self._plans.get(persisted_intent.intent_id)
        if existing_plan is not None:
            if existing_plan.planning_fingerprint != fingerprint:
                raise ResearchPlanningConflict(
                    f"research plan changed for intent {persisted_intent.intent_id}"
                )
            return existing_plan
        if decision.status is RiskDecisionStatus.REJECTED:
            plan = ResearchExecutionPlan(
                persisted_intent,
                decision,
                None,
                None,
                fingerprint,
            )
            self._plans[persisted_intent.intent_id] = plan
            return plan

        execution_payload = {
            "symbol": intent.symbol,
            "observed_at": planned_at,
            "risk_market_snapshot_id": risk_market.snapshot_id,
            "execution_price": str(selected_execution),
        }
        side = decision.approved_action.value
        buy = side in {"open_long", "close_short"}
        execution_market = ExecutionMarketSnapshot(
            snapshot_id="research_execution_market:" + self._fingerprint(execution_payload),
            symbol=intent.symbol,
            observed_at=planned_at,
            mark_price=float(risk_mark),
            bid_price=(float(risk_mark) if buy else float(selected_execution)),
            ask_price=(float(selected_execution) if buy else float(risk_mark)),
        )
        command = build_execution_command(
            ExecutionContext(
                self.execution_instance_id,
                decision,
                execution_market,
                self.execution_policy,
                planned_at,
            )
        )
        plan = ResearchExecutionPlan(
            persisted_intent,
            decision,
            execution_market,
            command,
            fingerprint,
        )
        self._plans[persisted_intent.intent_id] = plan
        return plan

    def cumulative_fill_update(
        self,
        *,
        order: ExecutionOrder,
        event_time: int,
        cumulative_quantity: Decimal | int | float | str,
        cumulative_quote_value: Decimal | int | float | str,
        cumulative_fee: Decimal | int | float | str,
        evidence: dict[str, Any] | None = None,
    ) -> OrderUpdate:
        quantity = as_decimal(cumulative_quantity, "cumulative_quantity")
        quote = as_decimal(cumulative_quote_value, "cumulative_quote_value")
        fee = as_decimal(cumulative_fee, "cumulative_fee")
        if min(quantity, quote, fee) < 0:
            raise ValueError("research cumulative fill values must be non-negative")
        requested = as_decimal(order.quantity, "order.quantity")
        if quantity > requested + Decimal("0.000000000001"):
            raise ValueError("research cumulative fill exceeds order quantity")
        if quantity == 0:
            if quote != 0:
                raise ValueError("zero research fill cannot contain quote value")
            state = OrderState.ACKNOWLEDGED
            average = None
        else:
            if quote <= 0:
                raise ValueError("positive research fill requires quote value")
            complete = abs(quantity - requested) <= Decimal("0.000000000001")
            state = OrderState.FILLED if complete else OrderState.PARTIALLY_FILLED
            average = quote / quantity
        payload = {
            "order_id": order.order_id,
            "event_time": event_time,
            "state": state.value,
            "cumulative_quantity": str(quantity),
            "cumulative_quote_value": str(quote),
            "cumulative_fee": str(fee),
            "evidence": evidence or {},
        }
        digest = self._fingerprint(payload)
        return OrderUpdate(
            update_id="research_order_update:" + digest,
            order_id=order.order_id,
            event_time=event_time,
            new_state=state,
            cumulative_filled_quantity=float(quantity),
            cumulative_quote_value=float(quote),
            cumulative_fee=float(fee),
            average_fill_price=None if average is None else float(average),
            broker_order_id="research-" + digest[:24],
            evidence={
                "source": "ResearchExecutionFactory",
                "submitted": False,
                "network_write": False,
                **(evidence or {}),
            },
        )
