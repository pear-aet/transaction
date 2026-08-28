from __future__ import annotations

from decimal import Decimal

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution import build_execution_command
from crypto_quant.execution_contracts import (
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    ExecutionOrder,
    OrderState,
    OrderUpdate,
)
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    PositionRiskSnapshot,
    RiskContext,
    RiskDecisionStatus,
    RiskPolicy,
)
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
DECISION_TIME = 1_000_000
NOW = 1_000_001


def market_rules(symbol: str = SYMBOL) -> MarketRules:
    return MarketRules(
        symbol,
        tick_size=0.1,
        step_size=0.001,
        min_quantity=0.001,
        min_notional=5,
        max_leverage=100,
        brackets=(RiskBracket(0, 1_000_000, 0.005),),
    )


def strategy_intent(action: StrategyAction = StrategyAction.ENTER_LONG) -> StrategyIntent:
    stop_price = 99 if action is StrategyAction.ENTER_LONG else None
    return StrategyIntent(
        intent_id=f"portfolio-intent-{action.value}",
        strategy_id="portfolio-test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=action,
        decision_time=DECISION_TIME,
        created_at=DECISION_TIME,
        reference_price=100,
        stop_price=stop_price,
        reason="portfolio_accounting_test",
        source_event_id=f"portfolio-market-{action.value}",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="portfolio-strategy-hash",
        evidence={},
        storage_sequence=1,
    )


def risk_decision(
    action: StrategyAction = StrategyAction.ENTER_LONG,
    *,
    storage_sequence: int = 1,
):
    position = None
    if action is StrategyAction.EXIT:
        position = PositionRiskSnapshot(
            SYMBOL,
            "long",
            quantity=2,
            entry_price=100,
            mark_price=100,
            initial_risk=20,
            maintenance_margin_rate=0.005,
            stop_price=90,
        )
    account = AccountRiskSnapshot(
        f"portfolio-account-{action.value}",
        "paper",
        NOW,
        10_000,
        10_000 if position is None else 9_900,
        0 if position is None else position.maintenance_margin_rate * position.notional,
        10_000,
        10_000,
        0,
        () if position is None else (position,),
    )
    market = MarketRiskSnapshot(
        f"portfolio-risk-market-{action.value}",
        SYMBOL,
        NOW,
        100,
        market_rules(),
    )
    decision = evaluate_risk(
        RiskContext(
            "risk-primary",
            strategy_intent(action),
            account,
            market,
            RiskPolicy(),
            NOW,
        )
    )
    assert decision.status is RiskDecisionStatus.APPROVED
    return decision.with_storage_sequence(storage_sequence)


def execution_command(decision=None):
    selected = decision or risk_decision()
    return build_execution_command(
        ExecutionContext(
            "execution-primary",
            selected,
            ExecutionMarketSnapshot(
                "portfolio-execution-market",
                selected.symbol,
                NOW,
                100,
                bid_price=99.9,
                ask_price=100.1,
            ),
            ExecutionPolicy(mode=ExecutionMode.SIMULATION),
            NOW,
        )
    )


def cumulative_update(
    order: ExecutionOrder,
    *,
    update_id: str,
    cumulative_quantity: Decimal,
    cumulative_quote_value: Decimal,
    cumulative_fee: Decimal,
    event_time: int,
    state: OrderState,
) -> OrderUpdate:
    average = (
        None
        if cumulative_quantity == 0
        else cumulative_quote_value / cumulative_quantity
    )
    return OrderUpdate(
        update_id=update_id,
        order_id=order.order_id,
        event_time=event_time,
        new_state=state,
        cumulative_filled_quantity=float(cumulative_quantity),
        cumulative_quote_value=float(cumulative_quote_value),
        cumulative_fee=float(cumulative_fee),
        average_fill_price=None if average is None else float(average),
        broker_order_id=f"sim-{order.client_order_id}",
        evidence={"source": "portfolio_fixture"},
    )
