import asyncio

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution_broker import SimulatedBroker
from crypto_quant.execution_contracts import (
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    OrderLegType,
    OrderState,
)
from crypto_quant.execution_engine import ExecutionEngine, ExecutionEngineSettings
from crypto_quant.execution_reconcile import (
    ExecutionReconciliationError,
    ObservedExecutionOrder,
)
from crypto_quant.execution_store import ExecutionDeliveryState, ExecutionStore
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskDecisionStatus,
    RiskPolicy,
)
from crypto_quant.risk_store import RiskStore
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
NOW = 900_001


def rules() -> MarketRules:
    return MarketRules(
        SYMBOL,
        tick_size=0.1,
        step_size=0.001,
        min_quantity=0.001,
        min_notional=5,
        max_leverage=100,
        brackets=(RiskBracket(0, 1_000_000, 0.005),),
    )


def setup_engine(tmp_path):
    risk_store = RiskStore(tmp_path / "risk.db")
    intent = StrategyIntent(
        intent_id="intent-reconciliation",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=900_000,
        created_at=900_000,
        reference_price=100,
        stop_price=99,
        reason="execution_reconciliation_engine_test",
        source_event_id="market-reconciliation",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=1,
    )
    decision = evaluate_risk(
        RiskContext(
            "risk-primary",
            intent,
            AccountRiskSnapshot(
                "account-reconciliation",
                "paper",
                900_000,
                10_000,
                10_000,
                0,
                10_000,
                10_000,
                0,
            ),
            MarketRiskSnapshot(
                "market-reconciliation",
                SYMBOL,
                900_000,
                100,
                rules(),
            ),
            RiskPolicy(),
            NOW,
        )
    )
    assert decision.status is RiskDecisionStatus.APPROVED
    stored_decision = risk_store.commit_evaluation(
        decision,
        updated_at=NOW,
    ).stored_decision.decision
    risk_store.mark_published(stored_decision.decision_id, NOW)

    execution_store = ExecutionStore(tmp_path / "execution.db")
    broker = SimulatedBroker()
    engine = ExecutionEngine(
        ExecutionEngineSettings("execution-primary", "paper"),
        ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        lambda risk_decision, now_ms: ExecutionMarketSnapshot(
            f"execution-market-{risk_decision.decision_id}-{now_ms}",
            risk_decision.symbol,
            now_ms,
            100,
            99.9,
            100.1,
        ),
        store=execution_store,
        risk_store=risk_store,
        broker=broker,
    )
    planned = asyncio.run(engine.plan_decision(stored_decision, now_ms=NOW + 1))
    command = execution_store.load_command(planned.command.command_id)
    assert command is not None
    assert command.delivery_state is ExecutionDeliveryState.DISPATCHED
    return engine, execution_store, broker, command


def observed_from(order, *, observed_at: int, state=None, filled=None, average=None):
    selected_state = order.state if state is None else state
    selected_filled = order.cumulative_filled_quantity if filled is None else filled
    selected_average = order.average_fill_price if average is None else average
    quote = 0 if selected_average is None else selected_filled * selected_average
    fee = order.cumulative_fee if selected_filled == order.cumulative_filled_quantity else 0.1
    return ObservedExecutionOrder(
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id or "sim-observed-order",
        state=selected_state,
        cumulative_filled_quantity=selected_filled,
        cumulative_quote_value=quote,
        cumulative_fee=fee,
        observed_at=observed_at,
        average_fill_price=selected_average,
        evidence={"submitted": False, "network_write": False},
    )


def test_exact_reconciliation_preserves_dispatched_command(tmp_path) -> None:
    engine, execution_store, _broker, stored = setup_engine(tmp_path)
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)

    report = asyncio.run(
        engine.reconcile_broker_ledger(
            stored.command.command_id,
            (observed_from(primary, observed_at=NOW + 2),),
            observed_at=NOW + 2,
        )
    )

    assert report.complete is True
    assert report.updates == ()
    refreshed = execution_store.load_command(stored.command.command_id)
    assert refreshed is not None
    assert refreshed.delivery_state is ExecutionDeliveryState.DISPATCHED
    assert engine.health.reconciliation_runs == 1
    assert engine.health.reconciliation_failures == 0


def test_reconciliation_applies_forward_fill_and_activates_protection(tmp_path) -> None:
    engine, execution_store, _broker, stored = setup_engine(tmp_path)
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)

    report = asyncio.run(
        engine.reconcile_broker_ledger(
            stored.command.command_id,
            (
                observed_from(
                    primary,
                    observed_at=NOW + 2,
                    state=OrderState.PARTIALLY_FILLED,
                    filled=primary.quantity / 2,
                    average=100.1,
                ),
            ),
            observed_at=NOW + 2,
        )
    )

    assert report.complete is True
    assert len(report.updates) == 1
    refreshed = execution_store.load_command(stored.command.command_id)
    assert refreshed is not None
    primary = next(
        order for order in refreshed.orders if order.leg_type is OrderLegType.PRIMARY
    )
    protective = next(
        order
        for order in refreshed.orders
        if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    assert primary.state is OrderState.PARTIALLY_FILLED
    assert protective.state is OrderState.ACKNOWLEDGED
    assert engine.health.reconciliation_updates == 1
    assert engine.health.protection_activations == 1


def test_reconciliation_divergence_marks_command_failed_before_raising(tmp_path) -> None:
    engine, execution_store, _broker, stored = setup_engine(tmp_path)
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    unknown = ObservedExecutionOrder(
        client_order_id="unknown_order_1",
        broker_order_id="unknown-broker-order",
        state=OrderState.ACKNOWLEDGED,
        cumulative_filled_quantity=0,
        cumulative_quote_value=0,
        cumulative_fee=0,
        observed_at=NOW + 2,
        evidence={"submitted": False, "network_write": False},
    )

    with pytest.raises(ExecutionReconciliationError, match="unknown_observed"):
        asyncio.run(
            engine.reconcile_broker_ledger(
                stored.command.command_id,
                (observed_from(primary, observed_at=NOW + 2), unknown),
                observed_at=NOW + 2,
            )
        )

    failed = execution_store.load_command(stored.command.command_id)
    assert failed is not None
    assert failed.delivery_state is ExecutionDeliveryState.FAILED
    assert failed.failure_reason == "reconciliation:unknown_observed"
    assert engine.health.reconciliation_failures == 1
    assert engine.health.commands_failed == 1
