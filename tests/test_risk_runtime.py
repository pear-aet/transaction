import asyncio

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.risk_bus import RiskDecisionBus
from crypto_quant.risk_contracts import AccountRiskSnapshot, MarketRiskSnapshot, RiskPolicy
from crypto_quant.risk_engine import RiskEngine, RiskEngineSettings
from crypto_quant.risk_runtime import RiskRuntime, RiskRuntimeSettings
from crypto_quant.risk_store import RiskStore
from crypto_quant.strategy_bus import StrategyIntentBus
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent
from crypto_quant.strategy_store import StrategyEvaluationMode, StrategyStore

SYMBOL = "BTC/USDT:USDT"
NOW = 900_001


def intent(index: int) -> StrategyIntent:
    decision_time = 900_000 + index - 1
    return StrategyIntent(
        intent_id=f"intent-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=decision_time,
        created_at=decision_time,
        reference_price=100,
        stop_price=99,
        reason="runtime_test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
    )


def persist_strategy_intent(store: StrategyStore, value: StrategyIntent) -> StrategyIntent:
    commit = store.commit_evaluation(
        instance_id=value.instance_id,
        symbol=value.symbol,
        event_time=value.decision_time,
        event_id=value.source_event_id,
        updated_at=value.created_at,
        strategy_id=value.strategy_id,
        strategy_version=value.strategy_version,
        parameter_hash=value.parameter_hash,
        high_timeframe=value.high_timeframe,
        low_timeframe=value.low_timeframe,
        intent=value,
        mode=StrategyEvaluationMode.LIVE,
        delivery_required=True,
    )
    return commit.stored_intent.intent


class Providers:
    def account(self, strategy_intent, now_ms):
        return AccountRiskSnapshot(
            f"account-{strategy_intent.intent_id}",
            "paper",
            now_ms - 1,
            10_000,
            10_000,
            0,
            10_000,
            10_000,
            0,
        )

    def market(self, strategy_intent, now_ms):
        rules = MarketRules(
            SYMBOL,
            0.1,
            0.001,
            0.001,
            5,
            100,
            (RiskBracket(0, 1_000_000, 0.005),),
        )
        return MarketRiskSnapshot(
            f"market-{strategy_intent.intent_id}",
            SYMBOL,
            now_ms - 1,
            100,
            rules,
        )


async def wait_for_bootstrap(runtime: RiskRuntime) -> None:
    for _ in range(500):
        if runtime.health.bootstrap_runs:
            return
        await asyncio.sleep(0.002)
    raise AssertionError("risk runtime did not complete bootstrap")


def test_runtime_catches_up_durable_intents_then_processes_live_bus(tmp_path) -> None:
    async def scenario() -> None:
        database = tmp_path / "quant.db"
        strategy_store = StrategyStore(database)
        first = persist_strategy_intent(strategy_store, intent(1))
        strategy_store.mark_published(first.intent_id, NOW)

        intent_bus = StrategyIntentBus()
        decision_bus = RiskDecisionBus()
        execution = await decision_bus.subscribe("execution")
        providers = Providers()
        risk_store = RiskStore(database)
        risk_engine = RiskEngine(
            RiskEngineSettings("risk-primary", "paper"),
            RiskPolicy(),
            providers.account,
            providers.market,
            store=risk_store,
            decision_bus=decision_bus,
        )
        runtime = RiskRuntime(
            intent_bus,
            strategy_store,
            risk_engine,
            RiskRuntimeSettings("risk-live", "risk-upstream-v1"),
            clock=lambda: NOW,
        )
        task = asyncio.create_task(runtime.run())
        await wait_for_bootstrap(runtime)

        recovered = await execution.get(timeout=1.0)
        assert recovered.intent_id == first.intent_id
        assert runtime.health.replayed_intents == 1
        assert (
            runtime.replay_ledger.offset(
                "risk-upstream-v1",
                runtime._durable_filter(),
            ).sequence
            == first.storage_sequence
        )

        second = persist_strategy_intent(strategy_store, intent(2))
        published = await intent_bus.publish(second)
        assert published.critical_delivered == 1
        strategy_store.mark_published(second.intent_id, NOW)
        live = await execution.get(timeout=1.0)
        assert live.intent_id == second.intent_id

        for _ in range(500):
            if runtime.health.live_intents == 1:
                break
            await asyncio.sleep(0.002)
        assert runtime.health.live_intents == 1
        assert risk_store.metrics()["decisions"] == 2

        await runtime.stop()
        await asyncio.wait_for(task, timeout=1.0)
        await runtime.close()
        await intent_bus.close()

    asyncio.run(scenario())
