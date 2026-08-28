import asyncio
from dataclasses import replace

import pytest

from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle, MarketRules, RiskBracket, Signal
from crypto_quant.engine import EngineState, EventEngine, Position
from crypto_quant.risk import RiskDecisionMismatch, evaluate_risk, validate_risk_decision
from crypto_quant.risk_bus import RiskDecisionBus
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskPolicy,
)
from crypto_quant.risk_engine import RiskDeliveryError, RiskEngine, RiskEngineSettings
from crypto_quant.risk_store import RiskDeliveryState, RiskStore
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"


def rules(symbol: str = BTC) -> MarketRules:
    return MarketRules(
        symbol,
        tick_size=0.1,
        step_size=0.001,
        min_quantity=0.001,
        min_notional=5,
        max_leverage=100,
        brackets=(RiskBracket(0, 1_000_000, 0.005),),
    )


def intent(index: int, decision_time: int) -> StrategyIntent:
    return StrategyIntent(
        intent_id=f"intent-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=BTC,
        action=StrategyAction.ENTER_LONG,
        decision_time=decision_time,
        created_at=decision_time,
        reference_price=100,
        stop_price=99,
        reason="final_recovery_test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=index,
    )


def risk_context(
    strategy_intent: StrategyIntent,
    *,
    observed_at: int,
    evaluated_at: int,
) -> RiskContext:
    account = AccountRiskSnapshot(
        f"account-{strategy_intent.intent_id}-{observed_at}",
        "paper",
        observed_at,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    market = MarketRiskSnapshot(
        f"market-{strategy_intent.intent_id}-{observed_at}",
        BTC,
        observed_at,
        100,
        rules(),
    )
    return RiskContext(
        "risk-primary",
        strategy_intent,
        account,
        market,
        RiskPolicy(),
        evaluated_at,
    )


def test_bootstrap_expires_later_stale_rows_before_valid_pending_blocks_delivery(tmp_path) -> None:
    async def scenario() -> None:
        store = RiskStore(tmp_path / "quant.db")
        valid_context = risk_context(
            intent(1, 1_000_000),
            observed_at=1_490_000,
            evaluated_at=1_490_000,
        )
        stale_context = risk_context(
            intent(2, 1_100_000),
            observed_at=1_100_000,
            evaluated_at=1_100_000,
        )
        valid = evaluate_risk(valid_context)
        stale = evaluate_risk(stale_context)
        assert valid.valid_until == 1_520_000
        assert stale.valid_until == 1_130_000
        store.commit_evaluation(valid, updated_at=1_490_000)
        store.commit_evaluation(stale, updated_at=1_100_000)

        engine = RiskEngine(
            RiskEngineSettings("risk-primary", "paper"),
            RiskPolicy(),
            lambda _intent, _now: valid_context.account,
            lambda _intent, _now: valid_context.market,
            store=store,
            decision_bus=RiskDecisionBus(),
        )

        with pytest.raises(RiskDeliveryError, match="no critical execution subscriber"):
            await engine.bootstrap(now_ms=1_500_000)
        first = store.load_by_intent("risk-primary", valid.intent_id)
        second = store.load_by_intent("risk-primary", stale.intent_id)
        assert first is not None and first.delivery_state is RiskDeliveryState.PENDING
        assert second is not None and second.delivery_state is RiskDeliveryState.EXPIRED
        assert store.metrics()["pending_decisions"] == 1
        assert store.metrics()["expired_decisions"] == 1
        await engine.close()

    asyncio.run(scenario())


def test_decision_identity_and_validity_are_recomputed_from_context() -> None:
    context = risk_context(
        intent(1, 1_000_000),
        observed_at=1_000_000,
        evaluated_at=1_000_000,
    )
    decision = evaluate_risk(context)

    with pytest.raises(RiskDecisionMismatch, match="decision_id"):
        validate_risk_decision(context, replace(decision, decision_id="wrong-id"))
    with pytest.raises(RiskDecisionMismatch, match="valid_until"):
        validate_risk_decision(
            context,
            replace(decision, valid_until=decision.valid_until + 1),
        )


def test_legacy_event_engine_uses_one_conservative_default_correlation_group() -> None:
    captured = []

    def evaluator(context: RiskContext):
        captured.append(context)
        return evaluate_risk(context)

    config = StrategyConfig(
        symbol=BTC,
        high_timeframe="1h",
        low_timeframe="15m",
        fee_rate=0,
        slippage_rate=0,
        fallback_funding_rate_8h=0,
        require_mark_price=False,
    )
    state = EngineState(10_000)
    state.positions[ETH] = Position(
        symbol=ETH,
        side="long",
        quantity=1,
        entry_price=100,
        stop_price=99,
        take_profit=102,
        entry_time=0,
        entry_fee=0,
        effective_leverage=1,
        initial_risk=25,
        maintenance_margin_rate=0.005,
    )
    state.latest_prices[ETH] = 100
    state.pending[BTC] = Signal(0, "enter_long", "group_test", 99)
    engine = EventEngine(config, state, risk_evaluator=evaluator)
    bar = Candle(0, 100, 101, 99.5, 100, 1)

    engine.process_bar(BTC, bar, [bar], [], generate_signal=False)

    assert len(captured) == 1
    context = captured[0]
    assert context.market.correlation_group == "default"
    assert {position.correlation_group for position in context.account.positions} == {"default"}
