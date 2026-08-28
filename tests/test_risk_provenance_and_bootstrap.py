import asyncio
from dataclasses import replace

import pytest

from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle, MarketRules, RiskBracket, Signal
from crypto_quant.engine import EngineState, EventEngine
from crypto_quant.risk import RiskDecisionMismatch, evaluate_risk
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

SYMBOL = "BTC/USDT:USDT"
NOW = 1_000_000


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


def intent() -> StrategyIntent:
    return StrategyIntent(
        intent_id="intent-1",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=900_000,
        created_at=900_000,
        reference_price=100,
        stop_price=99,
        reason="provenance_test",
        source_event_id="market-1",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=1,
    )


def account() -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        "account-snapshot",
        "paper",
        NOW,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )


def market() -> MarketRiskSnapshot:
    return MarketRiskSnapshot(
        "market-snapshot",
        SYMBOL,
        NOW,
        100,
        rules(),
    )


def test_custom_risk_evaluator_cannot_return_a_decision_for_another_intent(tmp_path) -> None:
    async def scenario() -> None:
        store = RiskStore(tmp_path / "quant.db")
        bus = RiskDecisionBus(require_critical_delivery=False)

        def mismatched(context: RiskContext):
            return replace(evaluate_risk(context), intent_id="different-intent")

        engine = RiskEngine(
            RiskEngineSettings(
                "risk-primary",
                "paper",
                require_execution_subscriber=False,
            ),
            RiskPolicy(),
            lambda _intent, _now: account(),
            lambda _intent, _now: market(),
            store=store,
            decision_bus=bus,
            evaluator=mismatched,
        )

        with pytest.raises(RiskDecisionMismatch, match="intent_id"):
            await engine.evaluate_intent(intent(), now_ms=NOW)
        assert store.metrics()["decisions"] == 0
        assert store.metrics()["evaluations"] == 0
        await engine.close()

    asyncio.run(scenario())


def test_event_engine_validates_custom_risk_decision_provenance() -> None:
    def mismatched(context: RiskContext):
        return replace(
            evaluate_risk(context),
            account_snapshot_id="different-account-snapshot",
        )

    config = StrategyConfig(
        symbol=SYMBOL,
        high_timeframe="1h",
        low_timeframe="15m",
        fee_rate=0,
        slippage_rate=0,
        fallback_funding_rate_8h=0,
        require_mark_price=False,
    )
    state = EngineState(10_000)
    engine = EventEngine(config, state, risk_evaluator=mismatched)
    state.pending[SYMBOL] = Signal(0, "enter_long", "valid_signal", 99)
    bar = Candle(0, 100, 101, 99.5, 100, 1)

    with pytest.raises(RiskDecisionMismatch, match="account_snapshot_id"):
        engine.process_bar(SYMBOL, bar, [bar], [], generate_signal=False)
    assert state.positions == {}


def test_bootstrap_expires_stale_pending_before_requiring_execution_subscriber(tmp_path) -> None:
    async def scenario() -> None:
        store = RiskStore(tmp_path / "quant.db")
        approved = evaluate_risk(
            RiskContext(
                "risk-primary",
                intent(),
                account(),
                market(),
                RiskPolicy(),
                NOW,
            )
        )
        store.commit_evaluation(approved, updated_at=NOW)
        engine = RiskEngine(
            RiskEngineSettings("risk-primary", "paper"),
            RiskPolicy(),
            lambda _intent, _now: account(),
            lambda _intent, _now: market(),
            store=store,
            decision_bus=RiskDecisionBus(),
        )

        with pytest.raises(RiskDeliveryError, match="no critical execution subscriber"):
            await engine.bootstrap(now_ms=approved.valid_until + 1)
        stored = store.load_by_intent("risk-primary", approved.intent_id)
        assert stored is not None
        assert stored.delivery_state is RiskDeliveryState.EXPIRED
        assert store.metrics()["pending_decisions"] == 0
        assert store.metrics()["expired_decisions"] == 1
        await engine.close()

    asyncio.run(scenario())
