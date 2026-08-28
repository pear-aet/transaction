import asyncio

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.market_bus import BackpressurePolicy
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_bus import RiskDecisionBus
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskDecisionFilter,
    RiskDecisionStatus,
    RiskPolicy,
)
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
NOW = 900_001


def decision(index: int = 1, *, market_price: float = 100):
    strategy_intent = StrategyIntent(
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
        reason="bus_test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="hash",
        evidence={},
        storage_sequence=index,
    )
    account = AccountRiskSnapshot(
        f"account-{index}",
        "paper",
        900_000,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    rules = MarketRules(
        SYMBOL,
        0.1,
        0.001,
        0.001,
        5,
        100,
        (RiskBracket(0, 1_000_000, 0.005),),
    )
    market = MarketRiskSnapshot(
        f"risk-market-{index}",
        SYMBOL,
        900_000,
        market_price,
        rules,
    )
    return evaluate_risk(
        RiskContext(
            "risk-primary",
            strategy_intent,
            account,
            market,
            RiskPolicy(),
            NOW,
        )
    ).with_storage_sequence(index)


async def wait_until_blocked(subscription) -> None:
    for _ in range(100):
        if (await subscription.snapshot())["blocked_count"]:
            return
        await asyncio.sleep(0)
    raise AssertionError("risk publisher did not enter backpressure wait")


def test_critical_bus_orders_persisted_approved_decisions_and_deduplicates() -> None:
    async def scenario() -> None:
        bus = RiskDecisionBus()
        execution = await bus.subscribe("execution", capacity=10)
        values = [decision(index) for index in range(1, 6)]
        results = await asyncio.gather(*(bus.publish(value, now_ms=NOW) for value in values))
        consumed = [await execution.get(timeout=1.0) for _ in values]

        assert [item.sequence for item in consumed] == list(range(1, 6))
        assert [result.decision.sequence for result in results if result.decision] == list(
            range(1, 6)
        )
        assert all(result.critical_delivered == 1 for result in results)
        duplicate = await bus.publish(values[0], now_ms=NOW)
        assert duplicate.duplicate is True
        assert duplicate.decision is None
        await bus.close()

    asyncio.run(scenario())


def test_approved_decision_is_rejected_without_matching_critical_execution() -> None:
    async def scenario() -> None:
        bus = RiskDecisionBus()
        dashboard = await bus.subscribe(
            "dashboard",
            capacity=1,
            policy=BackpressurePolicy.DROP_OLDEST,
            critical=False,
        )
        result = await bus.publish(decision(), now_ms=NOW)

        assert result.decision is None
        assert result.duplicate is False
        assert (await bus.health_snapshot())["rejected_no_critical"] == 1
        with pytest.raises(TimeoutError):
            await dashboard.get(timeout=0.01)
        with pytest.raises(ValueError, match="blocking"):
            await bus.subscribe(
                "unsafe-execution",
                policy=BackpressurePolicy.DISCONNECT,
                critical=True,
            )
        await bus.close()

    asyncio.run(scenario())


def test_blocking_execution_consumer_never_loses_a_decision() -> None:
    async def scenario() -> None:
        bus = RiskDecisionBus()
        execution = await bus.subscribe("execution", capacity=1)
        await bus.publish(decision(1), now_ms=NOW)
        pending = asyncio.create_task(bus.publish(decision(2), now_ms=NOW))
        await wait_until_blocked(execution)

        first = await execution.get(timeout=1.0)
        result = await asyncio.wait_for(pending, timeout=1.0)
        second = await execution.get(timeout=1.0)
        assert [first.storage_sequence, second.storage_sequence] == [1, 2]
        assert result.blocked_subscribers == 1
        assert result.dropped_oldest == 0
        await bus.close()

    asyncio.run(scenario())


def test_bus_rejects_rejected_unpersisted_sequenced_and_expired_decisions() -> None:
    async def scenario() -> None:
        bus = RiskDecisionBus()
        await bus.subscribe(
            "execution",
            decision_filter=RiskDecisionFilter(symbols=frozenset({SYMBOL})),
        )
        approved = decision()
        rejected = decision(market_price=110)
        assert rejected.status is RiskDecisionStatus.REJECTED

        with pytest.raises(ValueError, match="approved"):
            await bus.publish(rejected, now_ms=NOW)
        with pytest.raises(ValueError, match="persisted"):
            await bus.publish(replace_storage(approved, None), now_ms=NOW)
        with pytest.raises(ValueError, match="unsequenced"):
            await bus.publish(approved.with_sequence(9), now_ms=NOW)
        with pytest.raises(ValueError, match="expired"):
            await bus.publish(approved, now_ms=approved.valid_until + 1)
        await bus.close()

    asyncio.run(scenario())


def replace_storage(value, storage_sequence):
    from dataclasses import replace

    return replace(value, storage_sequence=storage_sequence)
