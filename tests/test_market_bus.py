import asyncio

import pytest

from crypto_quant.domain import Candle
from crypto_quant.market_bus import (
    BackpressurePolicy,
    MarketEventBus,
    MarketSubscription,
)
from crypto_quant.market_events import (
    MarketEvent,
    MarketEventFilter,
    MarketEventKind,
    closed_candle_market_event,
    mark_price_market_event,
)
from crypto_quant.market_stream import ClosedCandleEvent, MarkPriceEvent


def candle_event(index: int, symbol: str = "BTC/USDT:USDT") -> MarketEvent:
    timestamp = index * 900_000
    raw_symbol = symbol.split(":", 1)[0].replace("/", "")
    candle = Candle(
        timestamp,
        100.0 + index,
        102.0 + index,
        99.0 + index,
        101.0 + index,
        1.0,
    )
    raw = ClosedCandleEvent(
        raw_symbol,
        "15m",
        candle,
        timestamp + 899_999,
    )
    return closed_candle_market_event(
        raw,
        symbol,
        received_at=timestamp + 900_000,
    )


def mark_event(index: int = 1) -> MarketEvent:
    raw = MarkPriceEvent(
        "BTCUSDT",
        101.0,
        100.9,
        0.0001,
        3_600_000,
        index,
    )
    return mark_price_market_event(
        raw,
        "BTC/USDT:USDT",
        received_at=index + 1,
    )


async def wait_until_blocked(subscription: MarketSubscription) -> None:
    for _ in range(100):
        snapshot = await subscription.snapshot()
        if snapshot["blocked_count"]:
            return
        await asyncio.sleep(0)
    raise AssertionError("publisher did not enter backpressure wait")


def test_bus_assigns_one_global_order_and_suppresses_duplicates() -> None:
    async def scenario() -> None:
        bus = MarketEventBus(dedupe_capacity=100)
        subscription = await bus.subscribe("strategy", capacity=64)
        source_events = [candle_event(index) for index in range(1, 21)]
        publishers = [bus.publish(event) for event in source_events]

        results = await asyncio.gather(*publishers)
        consumed = [await subscription.get(timeout=1.0) for _ in source_events]
        expected_ids = {event.event_id for event in source_events}
        result_sequences = []
        for result in results:
            if result.event is not None:
                result_sequences.append(result.event.sequence)

        assert [event.sequence for event in consumed] == list(range(1, 21))
        assert {event.event_id for event in consumed} == expected_ids
        assert sorted(result_sequences) == list(range(1, 21))
        duplicate = await bus.publish(source_events[0])
        assert duplicate.duplicate is True
        assert duplicate.event is None
        assert (await bus.health_snapshot())["duplicates"] == 1

        with pytest.raises(ValueError, match="unsequenced"):
            await bus.publish(candle_event(30).with_sequence(99))
        await bus.close()
        with pytest.raises(StopAsyncIteration):
            await subscription.get()

    asyncio.run(scenario())


def test_partition_event_time_cannot_move_backwards() -> None:
    async def scenario() -> None:
        bus = MarketEventBus()
        subscription = await bus.subscribe("risk")
        latest = await bus.publish(mark_event(2))
        stale = await bus.publish(mark_event(1))

        assert latest.event is not None
        assert stale.stale is True
        assert stale.event is None
        assert (await subscription.get(timeout=1.0)).event_time == 2
        with pytest.raises(TimeoutError):
            await subscription.get(timeout=0.01)
        health = await bus.health_snapshot()
        assert health["stale"] == 1
        assert health["published"] == 1
        await bus.close()

    asyncio.run(scenario())


def test_block_policy_applies_backpressure_without_losing_events() -> None:
    async def scenario() -> None:
        bus = MarketEventBus()
        subscription = await bus.subscribe(
            "critical-strategy",
            capacity=1,
            policy=BackpressurePolicy.BLOCK,
        )
        await bus.publish(candle_event(1))
        pending = asyncio.create_task(bus.publish(candle_event(2)))
        await wait_until_blocked(subscription)

        first = await subscription.get(timeout=1.0)
        result = await asyncio.wait_for(pending, timeout=1.0)
        second = await subscription.get(timeout=1.0)
        assert first.source_time == 1_799_999
        assert second.source_time == 2_699_999
        assert first.event_time == 1_800_000
        assert second.event_time == 2_700_000
        assert result.blocked_subscribers == 1
        assert (await bus.health_snapshot())["dropped_oldest"] == 0
        await bus.close()

    asyncio.run(scenario())


def test_bus_close_wakes_a_blocked_publisher_and_drains_existing_events() -> None:
    async def scenario() -> None:
        bus = MarketEventBus()
        subscription = await bus.subscribe("critical", capacity=1)
        await bus.publish(candle_event(1))
        pending = asyncio.create_task(bus.publish(candle_event(2)))
        await wait_until_blocked(subscription)

        await bus.close()
        result = await asyncio.wait_for(pending, timeout=1.0)
        assert result.disconnected == 1
        assert (await subscription.get(timeout=1.0)).sequence == 1
        with pytest.raises(StopAsyncIteration):
            await subscription.get()
        with pytest.raises(RuntimeError, match="closed"):
            await bus.publish(candle_event(3))

    asyncio.run(scenario())


def test_drop_oldest_and_disconnect_policies_are_explicit() -> None:
    async def scenario() -> None:
        drop_bus = MarketEventBus()
        latest_only = await drop_bus.subscribe(
            "dashboard",
            capacity=2,
            policy=BackpressurePolicy.DROP_OLDEST,
        )
        await drop_bus.publish(candle_event(1))
        await drop_bus.publish(candle_event(2))
        third = await drop_bus.publish(candle_event(3))
        first_remaining = await latest_only.get(timeout=1.0)
        second_remaining = await latest_only.get(timeout=1.0)
        assert third.dropped_oldest == 1
        assert [first_remaining.sequence, second_remaining.sequence] == [2, 3]
        await drop_bus.close()

        disconnect_bus = MarketEventBus()
        slow = await disconnect_bus.subscribe(
            "slow-consumer",
            capacity=1,
            policy=BackpressurePolicy.DISCONNECT,
        )
        await disconnect_bus.publish(candle_event(10))
        disconnected = await disconnect_bus.publish(candle_event(11))
        assert disconnected.disconnected == 1
        assert slow.closed is True
        assert (await slow.get(timeout=1.0)).sequence == 1
        with pytest.raises(StopAsyncIteration):
            await slow.get()
        health = await disconnect_bus.health_snapshot()
        assert health["active_subscribers"] == 0
        await disconnect_bus.close()

    asyncio.run(scenario())


def test_subscription_filters_are_applied_before_queueing() -> None:
    async def scenario() -> None:
        bus = MarketEventBus()
        event_filter = MarketEventFilter(
            kinds=frozenset({MarketEventKind.CANDLE_CLOSED}),
            symbols=frozenset({"BTC/USDT:USDT"}),
            timeframes=frozenset({"15m"}),
            price_types=frozenset({"contract"}),
        )
        subscription = await bus.subscribe(
            "btc-15m-candles",
            event_filter=event_filter,
        )
        mark_result = await bus.publish(mark_event())
        eth_result = await bus.publish(candle_event(1, "ETH/USDT:USDT"))
        btc_result = await bus.publish(candle_event(2))

        assert mark_result.matched_subscribers == 0
        assert eth_result.matched_subscribers == 0
        assert btc_result.matched_subscribers == 1
        consumed = await subscription.get(timeout=1.0)
        assert consumed.symbol == "BTC/USDT:USDT"
        await bus.close()

    asyncio.run(scenario())
