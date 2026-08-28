import asyncio
import threading

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_bus import RiskDecisionBus
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskDecisionStatus,
    RiskPolicy,
)
from crypto_quant.risk_engine import (
    RiskDeliveryError,
    RiskEngine,
    RiskEngineSettings,
    RiskSnapshotError,
)
from crypto_quant.risk_store import RiskDeliveryState, RiskStore
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
NOW = 900_001


def intent(index: int = 1) -> StrategyIntent:
    return StrategyIntent(
        intent_id=f"intent-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=900_000,
        created_at=900_000,
        reference_price=100,
        stop_price=99,
        reason="engine_test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="hash",
        evidence={},
        storage_sequence=index,
    )


def rules() -> MarketRules:
    return MarketRules(
        SYMBOL,
        0.1,
        0.001,
        0.001,
        5,
        100,
        (RiskBracket(0, 1_000_000, 0.005),),
    )


class Providers:
    def __init__(
        self,
        *,
        market_price: float = 100,
        account_id: str = "paper",
        market_symbol: str = SYMBOL,
    ) -> None:
        self.market_price = market_price
        self.account_id = account_id
        self.market_symbol = market_symbol
        self.account_calls = 0
        self.market_calls = 0
        self.threads: list[int] = []

    def account(self, strategy_intent, now_ms):
        self.account_calls += 1
        self.threads.append(threading.get_ident())
        return AccountRiskSnapshot(
            f"account-{strategy_intent.intent_id}-{now_ms}",
            self.account_id,
            now_ms - 1,
            10_000,
            10_000,
            0,
            10_000,
            10_000,
            0,
        )

    def market(self, strategy_intent, now_ms):
        self.market_calls += 1
        self.threads.append(threading.get_ident())
        market_rules = rules()
        if self.market_symbol != SYMBOL:
            market_rules = MarketRules(
                self.market_symbol,
                market_rules.tick_size,
                market_rules.step_size,
                market_rules.min_quantity,
                market_rules.min_notional,
                market_rules.max_leverage,
                market_rules.brackets,
            )
        return MarketRiskSnapshot(
            f"market-{strategy_intent.intent_id}-{now_ms}",
            self.market_symbol,
            now_ms - 1,
            self.market_price,
            market_rules,
        )


def engine(tmp_path, providers, bus, *, require_execution=True):
    store = RiskStore(tmp_path / "quant.db")
    value = RiskEngine(
        RiskEngineSettings(
            "risk-primary",
            "paper",
            require_execution_subscriber=require_execution,
        ),
        RiskPolicy(),
        providers.account,
        providers.market,
        store=store,
        decision_bus=bus,
    )
    return value, store


def test_approved_decision_persists_before_critical_publication(tmp_path) -> None:
    async def scenario() -> None:
        main_thread = threading.get_ident()
        providers = Providers()
        bus = RiskDecisionBus()
        execution = await bus.subscribe("execution")
        risk_engine, store = engine(tmp_path, providers, bus)
        await risk_engine.bootstrap(now_ms=NOW)

        result = await risk_engine.evaluate_intent(intent(), now_ms=NOW)
        delivered = await execution.get(timeout=1.0)

        assert result.delivery_state is RiskDeliveryState.PUBLISHED
        assert result.decision.storage_sequence == 1
        assert delivered.decision_id == result.decision.decision_id
        assert delivered.sequence == 1
        assert store.metrics()["published_decisions"] == 1
        assert providers.account_calls == providers.market_calls == 1
        assert all(identifier != main_thread for identifier in providers.threads)
        await risk_engine.close()

    asyncio.run(scenario())


def test_missing_execution_persists_pending_decision_then_recovers(tmp_path) -> None:
    async def scenario() -> None:
        providers = Providers()
        bus = RiskDecisionBus()
        risk_engine, store = engine(tmp_path, providers, bus)

        with pytest.raises(RiskDeliveryError, match="no critical execution subscriber"):
            await risk_engine.evaluate_intent(intent(), now_ms=NOW)
        stored = store.load_by_intent("risk-primary", intent().intent_id)
        assert stored is not None
        assert stored.delivery_state is RiskDeliveryState.PENDING

        execution = await bus.subscribe("execution")
        recovered = await risk_engine.bootstrap(now_ms=NOW + 1)
        delivered = await execution.get(timeout=1.0)
        assert recovered.published == 1
        assert delivered.decision_id == stored.decision.decision_id
        assert store.metrics()["pending_decisions"] == 0
        await risk_engine.close()

    asyncio.run(scenario())


def test_bootstrap_expires_stale_pending_approval_instead_of_delivering(tmp_path) -> None:
    async def scenario() -> None:
        providers = Providers()
        bus = RiskDecisionBus()
        execution = await bus.subscribe("execution")
        risk_engine, store = engine(tmp_path, providers, bus)
        strategy_intent = intent()
        account = providers.account(strategy_intent, NOW)
        market = providers.market(strategy_intent, NOW)
        approved = evaluate_risk(
            RiskContext(
                "risk-primary",
                strategy_intent,
                account,
                market,
                RiskPolicy(),
                NOW,
            )
        )
        store.commit_evaluation(approved, updated_at=NOW)

        result = await risk_engine.bootstrap(now_ms=approved.valid_until + 1)
        assert result.expired == 1
        assert store.metrics()["expired_decisions"] == 1
        with pytest.raises(TimeoutError):
            await execution.get(timeout=0.01)
        await risk_engine.close()

    asyncio.run(scenario())


def test_duplicate_intent_reuses_stored_decision_without_new_snapshots(tmp_path) -> None:
    async def scenario() -> None:
        providers = Providers()
        bus = RiskDecisionBus()
        execution = await bus.subscribe("execution")
        risk_engine, _ = engine(tmp_path, providers, bus)
        first = await risk_engine.evaluate_intent(intent(), now_ms=NOW)
        await execution.get(timeout=1.0)
        second = await risk_engine.evaluate_intent(intent(), now_ms=NOW + 1)

        assert second.duplicate is True
        assert second.decision.decision_id == first.decision.decision_id
        assert providers.account_calls == providers.market_calls == 1
        await risk_engine.close()

    asyncio.run(scenario())


def test_rejected_decision_is_recorded_without_execution_subscriber(tmp_path) -> None:
    async def scenario() -> None:
        providers = Providers(market_price=110)
        bus = RiskDecisionBus(require_critical_delivery=False)
        risk_engine, store = engine(
            tmp_path,
            providers,
            bus,
            require_execution=False,
        )

        result = await risk_engine.evaluate_intent(intent(), now_ms=NOW)
        assert result.decision.status is RiskDecisionStatus.REJECTED
        assert result.delivery_state is RiskDeliveryState.RECORDED
        assert store.metrics()["recorded_decisions"] == 1
        await risk_engine.close()

    asyncio.run(scenario())


def test_snapshot_partition_mismatch_fails_before_persistence(tmp_path) -> None:
    async def scenario() -> None:
        providers = Providers(account_id="wrong-account")
        bus = RiskDecisionBus(require_critical_delivery=False)
        risk_engine, store = engine(
            tmp_path,
            providers,
            bus,
            require_execution=False,
        )

        with pytest.raises(RiskSnapshotError, match="expected paper"):
            await risk_engine.evaluate_intent(intent(), now_ms=NOW)
        assert store.metrics()["decisions"] == 0
        await risk_engine.close()

    asyncio.run(scenario())
