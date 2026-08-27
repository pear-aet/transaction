import asyncio

import pytest

from crypto_quant.market_bus import BackpressurePolicy
from crypto_quant.strategy_bus import StrategyIntentBus
from crypto_quant.strategy_contracts import (
    StrategyAction,
    StrategyIntent,
    StrategyIntentFilter,
)


def intent(index: int, symbol: str = "BTC/USDT:USDT") -> StrategyIntent:
    reference = 100.0 + index
    return StrategyIntent(
        intent_id=f"intent-{symbol}-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="primary",
        symbol=symbol,
        action=StrategyAction.ENTER_LONG,
        decision_time=index,
        created_at=index + 1,
        reference_price=reference,
        stop_price=reference - 1,
        reason="test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="hash",
        evidence={},
    )


async def wait_until_blocked(subscription) -> None:
    for _ in range(100):
        if (await subscription.snapshot())["blocked_count"]:
            return
        await asyncio.sleep(0)
    raise AssertionError("intent publisher did not block")


def test_intent_bus_preserves_global_order_and_suppresses_duplicates() -> None:
    async def scenario() -> None:
        bus = StrategyIntentBus()
        subscription = await bus.subscribe("risk", capacity=20)
        results = await asyncio.gather(*(bus.publish(intent(index)) for index in range(1, 11)))
        consumed = [await subscription.get(timeout=1.0) for _ in range(10)]
        assert [item.sequence for item in consumed] == list(range(1, 11))
        assert [result.intent.sequence for result in results if result.intent] == list(range(1, 11))
        duplicate = await bus.publish(intent(1))
        assert duplicate.duplicate is True
        assert duplicate.intent is None
        with pytest.raises(ValueError, match="unsequenced"):
            await bus.publish(intent(20).with_sequence(99))
        await bus.close()

    asyncio.run(scenario())


def test_intent_bus_block_policy_never_drops_critical_intents() -> None:
    async def scenario() -> None:
        bus = StrategyIntentBus()
        subscription = await bus.subscribe(
            "execution",
            capacity=1,
            policy=BackpressurePolicy.BLOCK,
        )
        await bus.publish(intent(1))
        pending = asyncio.create_task(bus.publish(intent(2)))
        await wait_until_blocked(subscription)
        first = await subscription.get(timeout=1.0)
        result = await asyncio.wait_for(pending, timeout=1.0)
        second = await subscription.get(timeout=1.0)
        assert [first.intent_id, second.intent_id] == [intent(1).intent_id, intent(2).intent_id]
        assert result.blocked_subscribers == 1
        assert (await bus.health_snapshot())["dropped_oldest"] == 0
        await bus.close()

    asyncio.run(scenario())


def test_intent_filters_and_noncritical_drop_policy_are_explicit() -> None:
    async def scenario() -> None:
        bus = StrategyIntentBus()
        filtered = await bus.subscribe(
            "btc",
            intent_filter=StrategyIntentFilter(symbols=frozenset({"BTC/USDT:USDT"})),
        )
        dashboard = await bus.subscribe(
            "dashboard",
            capacity=1,
            policy=BackpressurePolicy.DROP_OLDEST,
        )
        eth = await bus.publish(intent(1, "ETH/USDT:USDT"))
        await bus.publish(intent(2))
        latest = await bus.publish(intent(3))
        assert eth.matched_subscribers == 1
        assert (await filtered.get(timeout=1.0)).symbol == "BTC/USDT:USDT"
        with pytest.raises(TimeoutError):
            await filtered.get(timeout=0.01)
        assert (await dashboard.get(timeout=1.0)).intent_id == intent(3).intent_id
        assert latest.dropped_oldest == 1
        await bus.close()

    asyncio.run(scenario())
