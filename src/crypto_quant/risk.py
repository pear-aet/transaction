from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from .domain import MarketRules, RiskBracket, finite_json
from .risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    PositionRiskSnapshot,
    RiskApprovedAction,
    RiskContext,
    RiskDecision,
    RiskDecisionStatus,
    RiskPolicy,
    RiskReason,
    risk_policy_hash,
)
from .strategy_contracts import StrategyAction

_EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class _Candidate:
    quantity: float
    notional: float
    leverage: float
    required_margin: float
    bracket: RiskBracket
    leverage_cap: float
    liquidation_notional_cap: float


def _floor_step(value: float, step: float) -> float:
    return float(
        (Decimal(str(value)) / Decimal(str(step))).to_integral_value(ROUND_DOWN)
        * Decimal(str(step))
    )


def _ceil_integer(value: float) -> float:
    return float(Decimal(str(value)).to_integral_value(ROUND_CEILING))


def _round_stop(value: float, tick: float, side: str) -> float:
    rounding = ROUND_DOWN if side == "long" else ROUND_UP
    return float(
        (Decimal(str(value)) / Decimal(str(tick))).to_integral_value(rounding) * Decimal(str(tick))
    )


def _dedupe_reasons(values: list[RiskReason]) -> tuple[RiskReason, ...]:
    return tuple(dict.fromkeys(values))


def _current_buffer_ratio(account: AccountRiskSnapshot, policy: RiskPolicy) -> float:
    if account.gross_notional <= 0:
        return 1.0
    reserve = account.gross_notional * policy.liquidation_fee_rate
    return (account.equity - account.maintenance_margin - reserve) / account.gross_notional


def _validate_rules(rules: MarketRules) -> bool:
    if min(rules.tick_size, rules.step_size, rules.max_leverage) <= 0:
        return False
    if rules.min_quantity < 0 or rules.min_notional < 0 or not rules.brackets:
        return False
    ordered = tuple(sorted(rules.brackets, key=lambda item: item.notional_floor))
    if ordered != rules.brackets or not math.isclose(ordered[0].notional_floor, 0.0):
        return False
    for index, bracket in enumerate(ordered):
        if bracket.maintenance_amount < 0:
            return False
        if index and not math.isclose(
            ordered[index - 1].notional_cap,
            bracket.notional_floor,
            rel_tol=1e-12,
            abs_tol=1e-9,
        ):
            return False
    return True


class RiskDecisionMismatch(RuntimeError):
    """Raised when an evaluator returns a decision for another input context."""


def validate_risk_decision(context: RiskContext, decision: RiskDecision) -> RiskDecision:
    expected_policy_hash = risk_policy_hash(context.policy)
    checks = (
        ("engine_instance_id", decision.engine_instance_id, context.engine_instance_id),
        ("policy_id", decision.policy_id, context.policy.policy_id),
        ("policy_version", decision.policy_version, context.policy.version),
        ("policy_hash", decision.policy_hash, expected_policy_hash),
        ("account_id", decision.account_id, context.account.account_id),
        ("account_snapshot_id", decision.account_snapshot_id, context.account.snapshot_id),
        ("market_snapshot_id", decision.market_snapshot_id, context.market.snapshot_id),
        ("intent_id", decision.intent_id, context.intent.intent_id),
        (
            "intent_storage_sequence",
            decision.intent_storage_sequence,
            context.intent.storage_sequence,
        ),
        ("symbol", decision.symbol, context.intent.symbol),
        ("requested_action", decision.requested_action, context.intent.action),
        ("decision_time", decision.decision_time, context.intent.decision_time),
        ("evaluated_at", decision.evaluated_at, context.evaluated_at),
    )
    mismatches = [name for name, actual, expected in checks if actual != expected]
    if not math.isclose(
        decision.reference_price,
        context.intent.reference_price,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        mismatches.append("reference_price")
    if decision.status is RiskDecisionStatus.REJECTED:
        expected_valid_until = context.evaluated_at
    elif decision.requested_action is StrategyAction.EXIT:
        expected_valid_until = context.intent.decision_time + context.policy.max_intent_age_ms
    else:
        expected_valid_until = min(
            context.intent.decision_time + context.policy.max_intent_age_ms,
            context.account.observed_at + context.policy.max_account_age_ms,
            context.market.observed_at + context.policy.max_market_age_ms,
        )
    if decision.valid_until != expected_valid_until:
        mismatches.append("valid_until")
    expected_decision_id = _decision_id(
        context,
        status=decision.status,
        approved_action=decision.approved_action,
        reasons=decision.reasons,
        quantity=decision.quantity,
        notional=decision.notional,
        leverage=decision.leverage,
        stop_price=decision.stop_price,
        valid_until=decision.valid_until,
    )
    if decision.decision_id != expected_decision_id:
        mismatches.append("decision_id")
    if mismatches:
        raise RiskDecisionMismatch("risk decision provenance mismatch: " + ", ".join(mismatches))
    return decision


def _decision_id(
    context: RiskContext,
    *,
    status: RiskDecisionStatus,
    approved_action: RiskApprovedAction | None,
    reasons: tuple[RiskReason, ...],
    quantity: float,
    notional: float,
    leverage: float,
    stop_price: float | None,
    valid_until: int,
) -> str:
    canonical = json.dumps(
        {
            "engine_instance_id": context.engine_instance_id,
            "policy_hash": risk_policy_hash(context.policy),
            "account_snapshot_id": context.account.snapshot_id,
            "market_snapshot_id": context.market.snapshot_id,
            "intent_id": context.intent.intent_id,
            "status": status.value,
            "approved_action": None if approved_action is None else approved_action.value,
            "reasons": [reason.value for reason in reasons],
            "quantity": format(quantity, ".17g"),
            "notional": format(notional, ".17g"),
            "leverage": format(leverage, ".17g"),
            "stop_price": None if stop_price is None else format(stop_price, ".17g"),
            "valid_until": valid_until,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.blake2s(canonical.encode("utf-8"), digest_size=16).hexdigest()
    return f"risk_decision:{digest}"


def _reject(
    context: RiskContext,
    reasons: list[RiskReason],
    *,
    evidence: dict[str, Any] | None = None,
    approval_price: float | None = None,
) -> RiskDecision:
    resolved_reasons = _dedupe_reasons(reasons)
    market_price = context.market.mark_price if approval_price is None else approval_price
    valid_until = context.evaluated_at
    decision_id = _decision_id(
        context,
        status=RiskDecisionStatus.REJECTED,
        approved_action=None,
        reasons=resolved_reasons,
        quantity=0.0,
        notional=0.0,
        leverage=0.0,
        stop_price=context.intent.stop_price,
        valid_until=valid_until,
    )
    account = context.account
    group = context.market.correlation_group
    return RiskDecision(
        decision_id=decision_id,
        engine_instance_id=context.engine_instance_id,
        policy_id=context.policy.policy_id,
        policy_version=context.policy.version,
        policy_hash=risk_policy_hash(context.policy),
        account_id=account.account_id,
        account_snapshot_id=account.snapshot_id,
        market_snapshot_id=context.market.snapshot_id,
        intent_id=context.intent.intent_id,
        intent_storage_sequence=context.intent.storage_sequence,
        symbol=context.intent.symbol,
        requested_action=context.intent.action,
        status=RiskDecisionStatus.REJECTED,
        approved_action=None,
        decision_time=context.intent.decision_time,
        evaluated_at=context.evaluated_at,
        valid_until=valid_until,
        reasons=resolved_reasons,
        reference_price=context.intent.reference_price,
        approval_price=market_price,
        stop_price=context.intent.stop_price,
        quantity=0.0,
        notional=0.0,
        leverage=0.0,
        required_margin=0.0,
        reduce_only=False,
        risk_budget=0.0,
        initial_risk=0.0,
        symbol_risk_after=account.symbol_risk(context.intent.symbol),
        portfolio_risk_after=account.open_risk,
        group_risk_after=account.group_risk(group),
        gross_notional_after=account.gross_notional,
        directional_notional_after=0.0,
        maintenance_margin_after=account.maintenance_margin,
        liquidation_buffer_ratio=_current_buffer_ratio(account, context.policy),
        evidence=finite_json(evidence or {}),
    )


def _approve_exit(
    context: RiskContext,
    position: PositionRiskSnapshot,
    warnings: list[str],
) -> RiskDecision:
    account = context.account
    policy = context.policy
    action = (
        RiskApprovedAction.CLOSE_LONG if position.side == "long" else RiskApprovedAction.CLOSE_SHORT
    )
    quantity = position.quantity
    notional = position.notional
    portfolio_risk_after = max(0.0, account.open_risk - position.initial_risk)
    group_risk_after = max(
        0.0,
        account.group_risk(position.correlation_group) - position.initial_risk,
    )
    gross_after = max(0.0, account.gross_notional - notional)
    directional_after = max(0.0, account.directional_notional(position.side) - notional)
    maintenance_after = max(
        0.0,
        account.maintenance_margin - notional * position.maintenance_margin_rate,
    )
    buffer_ratio = (
        1.0
        if gross_after <= 0
        else (account.equity - maintenance_after - gross_after * policy.liquidation_fee_rate)
        / gross_after
    )
    reasons = (RiskReason.APPROVED_EXIT,)
    valid_until = context.intent.decision_time + policy.max_intent_age_ms
    decision_id = _decision_id(
        context,
        status=RiskDecisionStatus.APPROVED,
        approved_action=action,
        reasons=reasons,
        quantity=quantity,
        notional=notional,
        leverage=1.0,
        stop_price=None,
        valid_until=valid_until,
    )
    return RiskDecision(
        decision_id=decision_id,
        engine_instance_id=context.engine_instance_id,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_hash=risk_policy_hash(policy),
        account_id=account.account_id,
        account_snapshot_id=account.snapshot_id,
        market_snapshot_id=context.market.snapshot_id,
        intent_id=context.intent.intent_id,
        intent_storage_sequence=context.intent.storage_sequence,
        symbol=context.intent.symbol,
        requested_action=context.intent.action,
        status=RiskDecisionStatus.APPROVED,
        approved_action=action,
        decision_time=context.intent.decision_time,
        evaluated_at=context.evaluated_at,
        valid_until=valid_until,
        reasons=reasons,
        reference_price=context.intent.reference_price,
        approval_price=context.market.mark_price,
        stop_price=None,
        quantity=quantity,
        notional=notional,
        leverage=1.0,
        required_margin=0.0,
        reduce_only=True,
        risk_budget=0.0,
        initial_risk=0.0,
        symbol_risk_after=0.0,
        portfolio_risk_after=portfolio_risk_after,
        group_risk_after=group_risk_after,
        gross_notional_after=gross_after,
        directional_notional_after=directional_after,
        maintenance_margin_after=maintenance_after,
        liquidation_buffer_ratio=buffer_ratio,
        evidence={
            "risk_reducing_exit": True,
            "warnings": warnings,
            "position": position.to_dict(),
        },
    )


def _volatility_scale(market: MarketRiskSnapshot, policy: RiskPolicy) -> float:
    if market.volatility_annualized is None:
        return 1.0
    raw = policy.volatility_target_annualized / market.volatility_annualized
    return max(policy.min_volatility_risk_scale, min(1.0, raw))


def _candidate_for_bracket(
    context: RiskContext,
    bracket: RiskBracket,
    *,
    fill_price: float,
    risk_quantity: float,
    gross_room: float,
    directional_room: float,
    volatility_scale: float,
    stop_distance_ratio: float,
) -> _Candidate | None:
    account = context.account
    market = context.market
    policy = context.policy
    cost_ratio = 2 * policy.fee_rate + policy.slippage_rate
    safety_denominator = (
        stop_distance_ratio
        + cost_ratio
        + bracket.maintenance_margin_rate
        + policy.liquidation_fee_rate
        + policy.min_liquidation_buffer
    )
    if safety_denominator <= 0:
        return None
    stop_leverage_cap = 1.0 / safety_denominator
    volatility_leverage_cap = policy.max_leverage * volatility_scale
    leverage_cap = min(
        policy.max_leverage,
        market.rules.max_leverage,
        stop_leverage_cap,
        volatility_leverage_cap,
    )
    if leverage_cap < 1:
        return None

    buffer_numerator = (
        account.equity
        - account.maintenance_margin
        - account.gross_notional * (policy.liquidation_fee_rate + policy.min_liquidation_buffer)
    )
    buffer_denominator = (
        policy.fee_rate
        + bracket.maintenance_margin_rate
        + policy.liquidation_fee_rate
        + policy.min_liquidation_buffer
    )
    liquidation_notional_cap = max(0.0, buffer_numerator / buffer_denominator)
    usable_balance = max(0.0, account.available_balance)
    notional_cap = min(
        risk_quantity * fill_price,
        gross_room,
        directional_room,
        account.equity * leverage_cap,
        usable_balance * leverage_cap,
        liquidation_notional_cap,
        bracket.notional_cap,
    )
    quantity = _floor_step(notional_cap / fill_price, market.rules.step_size)
    notional = quantity * fill_price
    if quantity <= 0 or notional <= 0:
        return None
    if notional + _EPSILON < bracket.notional_floor or notional > bracket.notional_cap + _EPSILON:
        return None
    fee = notional * policy.fee_rate
    margin_balance = account.available_balance - fee
    if margin_balance <= 0:
        return None
    minimum_leverage = notional / margin_balance
    leverage = max(1.0, _ceil_integer(minimum_leverage))
    if leverage > leverage_cap + _EPSILON:
        return None
    required_margin = notional / leverage + fee
    if required_margin > account.available_balance + _EPSILON:
        return None
    return _Candidate(
        quantity,
        notional,
        leverage,
        required_margin,
        bracket,
        leverage_cap,
        liquidation_notional_cap,
    )


def evaluate_risk(context: RiskContext) -> RiskDecision:
    intent = context.intent
    account = context.account
    market = context.market
    policy = context.policy

    warnings: list[str] = []
    if intent.action is StrategyAction.EXIT:
        exit_reasons: list[RiskReason] = []
        if intent.decision_time > context.evaluated_at:
            exit_reasons.append(RiskReason.FUTURE_INTENT)
        if context.evaluated_at - intent.decision_time > policy.max_intent_age_ms:
            exit_reasons.append(RiskReason.STALE_INTENT)
        if exit_reasons:
            return _reject(context, exit_reasons)
        position = account.position(intent.symbol)
        if position is None:
            return _reject(context, [RiskReason.NO_OPEN_POSITION])
        if context.evaluated_at - market.observed_at > policy.max_market_age_ms:
            warnings.append("market_snapshot_stale_but_exit_allowed")
        if context.evaluated_at - account.observed_at > policy.max_account_age_ms:
            warnings.append("account_snapshot_stale_but_exit_allowed")
        return _approve_exit(context, position, warnings)

    reasons: list[RiskReason] = []
    if account.global_halt:
        reasons.append(RiskReason.ACCOUNT_GLOBAL_HALT)
    if account.day_halt:
        reasons.append(RiskReason.ACCOUNT_DAY_HALT)
    if account.drawdown_halt:
        reasons.append(RiskReason.ACCOUNT_DRAWDOWN_HALT)
    if account.equity <= 0:
        reasons.append(RiskReason.NON_POSITIVE_EQUITY)
    if account.available_balance <= 0:
        reasons.append(RiskReason.INSUFFICIENT_AVAILABLE_BALANCE)
    if account.position(intent.symbol) is not None:
        reasons.append(RiskReason.SYMBOL_POSITION_EXISTS)
    if account.observed_at > context.evaluated_at or market.observed_at > context.evaluated_at:
        reasons.append(RiskReason.FUTURE_SNAPSHOT)
    if intent.decision_time > context.evaluated_at:
        reasons.append(RiskReason.FUTURE_INTENT)
    if context.evaluated_at - intent.decision_time > policy.max_intent_age_ms:
        reasons.append(RiskReason.STALE_INTENT)
    if context.evaluated_at - account.observed_at > policy.max_account_age_ms:
        reasons.append(RiskReason.STALE_ACCOUNT)
    if context.evaluated_at - market.observed_at > policy.max_market_age_ms:
        reasons.append(RiskReason.STALE_MARKET)
    if account.day_start_equity > 0:
        daily_loss = max(0.0, 1 - account.equity / account.day_start_equity)
        if daily_loss >= policy.daily_loss_limit:
            reasons.append(RiskReason.DAILY_LOSS_LIMIT)
    else:
        daily_loss = 0.0
    if account.peak_equity > 0:
        drawdown = max(0.0, 1 - account.equity / account.peak_equity)
        if drawdown >= policy.max_account_drawdown:
            reasons.append(RiskReason.ACCOUNT_DRAWDOWN_LIMIT)
    else:
        drawdown = 0.0
    if account.consecutive_losses >= policy.max_consecutive_losses:
        reasons.append(RiskReason.CONSECUTIVE_LOSS_LIMIT)
    reference_deviation = abs(market.mark_price - intent.reference_price) / intent.reference_price
    if reference_deviation > policy.max_reference_deviation:
        reasons.append(RiskReason.REFERENCE_DEVIATION)
    if not _validate_rules(market.rules):
        reasons.append(RiskReason.INVALID_MARKET_RULES)

    side = "long" if intent.action is StrategyAction.ENTER_LONG else "short"
    fill_price = market.mark_price * (
        1 + policy.slippage_rate if side == "long" else 1 - policy.slippage_rate
    )
    raw_stop = float(intent.stop_price or 0.0)
    stop_price = (
        _round_stop(raw_stop, market.rules.tick_size, side)
        if raw_stop > 0 and market.rules.tick_size > 0
        else raw_stop
    )
    price_risk = fill_price - stop_price if side == "long" else stop_price - fill_price
    if price_risk <= 0:
        reasons.append(RiskReason.INVALID_STOP)

    if reasons:
        return _reject(
            context,
            reasons,
            approval_price=fill_price,
            evidence={
                "daily_loss": daily_loss,
                "drawdown": drawdown,
                "reference_deviation": reference_deviation,
                "rounded_stop": stop_price,
            },
        )

    volatility_scale = _volatility_scale(market, policy)
    base_budget = account.equity * policy.risk_per_trade * volatility_scale
    symbol_room = account.equity * policy.max_symbol_risk - account.symbol_risk(intent.symbol)
    portfolio_room = account.equity * policy.max_portfolio_risk - account.open_risk
    group_room = account.equity * policy.max_group_risk - account.group_risk(
        market.correlation_group
    )
    if symbol_room <= 0:
        reasons.append(RiskReason.SYMBOL_RISK_LIMIT)
    if portfolio_room <= 0:
        reasons.append(RiskReason.PORTFOLIO_RISK_LIMIT)
    if group_room <= 0:
        reasons.append(RiskReason.GROUP_RISK_LIMIT)
    gross_room = account.equity * policy.max_gross_notional_multiplier - account.gross_notional
    directional_room = (
        account.equity * policy.max_directional_notional_multiplier
        - account.directional_notional(side)
    )
    if gross_room <= 0:
        reasons.append(RiskReason.GROSS_NOTIONAL_LIMIT)
    if directional_room <= 0:
        reasons.append(RiskReason.DIRECTIONAL_NOTIONAL_LIMIT)
    if reasons:
        return _reject(
            context,
            reasons,
            approval_price=fill_price,
            evidence={
                "base_budget": base_budget,
                "symbol_room": symbol_room,
                "portfolio_room": portfolio_room,
                "group_room": group_room,
                "gross_room": gross_room,
                "directional_room": directional_room,
                "volatility_scale": volatility_scale,
            },
        )

    risk_budget = min(base_budget, symbol_room, portfolio_room, group_room)
    cost_per_unit = fill_price * (2 * policy.fee_rate + policy.slippage_rate)
    risk_per_unit = price_risk + cost_per_unit
    risk_quantity = risk_budget / risk_per_unit
    stop_distance_ratio = price_risk / fill_price
    candidates = [
        candidate
        for bracket in market.rules.brackets
        if (
            candidate := _candidate_for_bracket(
                context,
                bracket,
                fill_price=fill_price,
                risk_quantity=risk_quantity,
                gross_room=gross_room,
                directional_room=directional_room,
                volatility_scale=volatility_scale,
                stop_distance_ratio=stop_distance_ratio,
            )
        )
        is not None
    ]
    if not candidates:
        theoretical_notional = risk_quantity * fill_price
        failure = RiskReason.MARKET_MINIMUM
        if theoretical_notional >= market.rules.min_notional:
            failure = RiskReason.LIQUIDATION_BUFFER
        return _reject(
            context,
            [failure],
            approval_price=fill_price,
            evidence={
                "risk_budget": risk_budget,
                "risk_per_unit": risk_per_unit,
                "theoretical_notional": theoretical_notional,
                "gross_room": gross_room,
                "directional_room": directional_room,
                "volatility_scale": volatility_scale,
            },
        )

    selected = max(candidates, key=lambda candidate: candidate.notional)
    if (
        selected.quantity < market.rules.min_quantity
        or selected.notional < market.rules.min_notional
    ):
        return _reject(
            context,
            [RiskReason.MARKET_MINIMUM],
            approval_price=fill_price,
            evidence={
                "quantity": selected.quantity,
                "notional": selected.notional,
                "min_quantity": market.rules.min_quantity,
                "min_notional": market.rules.min_notional,
            },
        )

    initial_risk = selected.quantity * risk_per_unit
    symbol_risk_after = account.symbol_risk(intent.symbol) + initial_risk
    portfolio_risk_after = account.open_risk + initial_risk
    group_risk_after = account.group_risk(market.correlation_group) + initial_risk
    gross_after = account.gross_notional + selected.notional
    directional_after = account.directional_notional(side) + selected.notional
    new_maintenance = max(
        0.0,
        selected.notional * selected.bracket.maintenance_margin_rate
        - selected.bracket.maintenance_amount,
    )
    maintenance_after = account.maintenance_margin + new_maintenance
    entry_fee = selected.notional * policy.fee_rate
    projected_equity = account.equity - entry_fee
    liquidation_buffer_ratio = (
        projected_equity - maintenance_after - gross_after * policy.liquidation_fee_rate
    ) / gross_after
    if liquidation_buffer_ratio + _EPSILON < policy.min_liquidation_buffer:
        return _reject(
            context,
            [RiskReason.LIQUIDATION_BUFFER],
            approval_price=fill_price,
            evidence={
                "liquidation_buffer_ratio": liquidation_buffer_ratio,
                "required_buffer": policy.min_liquidation_buffer,
            },
        )
    if initial_risk > risk_budget + max(_EPSILON, risk_budget * 1e-9):
        raise RuntimeError("rounded risk quantity exceeded its approved budget")

    action = RiskApprovedAction.OPEN_LONG if side == "long" else RiskApprovedAction.OPEN_SHORT
    reasons_tuple = (RiskReason.APPROVED_ENTRY,)
    valid_until = min(
        intent.decision_time + policy.max_intent_age_ms,
        account.observed_at + policy.max_account_age_ms,
        market.observed_at + policy.max_market_age_ms,
    )
    decision_id = _decision_id(
        context,
        status=RiskDecisionStatus.APPROVED,
        approved_action=action,
        reasons=reasons_tuple,
        quantity=selected.quantity,
        notional=selected.notional,
        leverage=selected.leverage,
        stop_price=stop_price,
        valid_until=valid_until,
    )
    return RiskDecision(
        decision_id=decision_id,
        engine_instance_id=context.engine_instance_id,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_hash=risk_policy_hash(policy),
        account_id=account.account_id,
        account_snapshot_id=account.snapshot_id,
        market_snapshot_id=market.snapshot_id,
        intent_id=intent.intent_id,
        intent_storage_sequence=intent.storage_sequence,
        symbol=intent.symbol,
        requested_action=intent.action,
        status=RiskDecisionStatus.APPROVED,
        approved_action=action,
        decision_time=intent.decision_time,
        evaluated_at=context.evaluated_at,
        valid_until=valid_until,
        reasons=reasons_tuple,
        reference_price=intent.reference_price,
        approval_price=fill_price,
        stop_price=stop_price,
        quantity=selected.quantity,
        notional=selected.notional,
        leverage=selected.leverage,
        required_margin=selected.required_margin,
        reduce_only=False,
        risk_budget=risk_budget,
        initial_risk=initial_risk,
        symbol_risk_after=symbol_risk_after,
        portfolio_risk_after=portfolio_risk_after,
        group_risk_after=group_risk_after,
        gross_notional_after=gross_after,
        directional_notional_after=directional_after,
        maintenance_margin_after=maintenance_after,
        liquidation_buffer_ratio=liquidation_buffer_ratio,
        evidence=finite_json(
            {
                "side": side,
                "price_risk": price_risk,
                "risk_per_unit": risk_per_unit,
                "volatility_scale": volatility_scale,
                "base_budget": base_budget,
                "symbol_room": symbol_room,
                "portfolio_room": portfolio_room,
                "group_room": group_room,
                "gross_room": gross_room,
                "directional_room": directional_room,
                "leverage_cap": selected.leverage_cap,
                "liquidation_notional_cap": selected.liquidation_notional_cap,
                "maintenance_margin_rate": selected.bracket.maintenance_margin_rate,
                "entry_fee": entry_fee,
            }
        ),
    )
