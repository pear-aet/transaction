from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .market_events import MarketEvent, MarketEventFilter


class BackpressurePolicy(StrEnum):
    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"
    DISCONNECT = "disconnect"


@dataclass(frozen=True, slots=True)
class PublishResult:
    event: MarketEvent | None
    duplicate: bool
    matched_subscribers: int
    delivered: int
    dropped_oldest: int
    disconnected: int
    blocked_subscribers: int


@dataclass(frozen=True, slots=True)
class _DeliveryOutcome:
    matched: bool = False
    delivered: bool = False
    dropped_oldest: bool = False
    disconnected: bool = False
    blocked: bool = False


class _SubscriberState:
    def __init__(
        self,
        name: str,
        event_filter: MarketEventFilter,
        capacity: int,
        policy: BackpressurePolicy,
    ) -> None:
        self.name = name
        self.event_filter = event_filter
        self.capacity = capacity
        self.policy = policy
        self._events: deque[MarketEvent] = deque()
        self._condition = asyncio.Condition()
        self.closed = False
        self.delivered = 0
        self.consumed = 0
        self.dropped_oldest = 0
        self.blocked_count = 0
        self.last_enqueued_sequence = 0
        self.last_consumed_sequence = 0

    async def enqueue(self, event: MarketEvent) -> _DeliveryOutcome:
        if not self.event_filter.matches(event):
            return _DeliveryOutcome()
        async with self._condition:
            if self.closed:
                return _DeliveryOutcome(matched=True, disconnected=True)
            blocked = False
            dropped = False
            if self.policy is BackpressurePolicy.BLOCK:
                if len(self._events) >= self.capacity:
                    blocked = True
                    self.blocked_count += 1
                while len(self._events) >= self.capacity and not self.closed:
                    await self._condition.wait()
                if self.closed:
                    return _DeliveryOutcome(
                        matched=True,
                        disconnected=True,
                        blocked=blocked,
                    )
            elif len(self._events) >= self.capacity:
                if self.policy is BackpressurePolicy.DROP_OLDEST:
                    self._events.popleft()
                    self.dropped_oldest += 1
                    dropped = True
                else:
                    self.closed = True
                    self._condition.notify_all()
                    return _DeliveryOutcome(matched=True, disconnected=True)

            self._events.append(event)
            self.delivered += 1
            self.last_enqueued_sequence = event.sequence
            self._condition.notify_all()
            return _DeliveryOutcome(
                matched=True,
                delivered=True,
                dropped_oldest=dropped,
                blocked=blocked,
            )

    async def get(self) -> MarketEvent:
        async with self._condition:
            while not self._events and not self.closed:
                await self._condition.wait()
            if not self._events:
                raise StopAsyncIteration
            event = self._events.popleft()
            self.consumed += 1
            self.last_consumed_sequence = event.sequence
            self._condition.notify_all()
            return event

    async def close(self, *, clear: bool) -> None:
        async with self._condition:
            self.closed = True
            if clear:
                self._events.clear()
            self._condition.notify_all()

    async def snapshot(self) -> dict[str, Any]:
        async with self._condition:
            return {
                "name": self.name,
                "policy": self.policy.value,
                "capacity": self.capacity,
                "queue_depth": len(self._events),
                "closed": self.closed,
                "delivered": self.delivered,
                "consumed": self.consumed,
                "dropped_oldest": self.dropped_oldest,
                "blocked_count": self.blocked_count,
                "last_enqueued_sequence": self.last_enqueued_sequence,
                "last_consumed_sequence": self.last_consumed_sequence,
            }


class MarketSubscription:
    def __init__(self, bus: MarketEventBus, state: _SubscriberState) -> None:
        self._bus = bus
        self._state = state

    @property
    def name(self) -> str:
        return self._state.name

    @property
    def closed(self) -> bool:
        return self._state.closed

    def __aiter__(self) -> MarketSubscription:
        return self

    async def __anext__(self) -> MarketEvent:
        return await self._state.get()

    async def get(self, timeout: float | None = None) -> MarketEvent:
        if timeout is None:
            return await self._state.get()
        return await asyncio.wait_for(self._state.get(), timeout=timeout)

    async def close(self) -> None:
        await self._bus.unsubscribe(self.name)

    async def snapshot(self) -> dict[str, Any]:
        return await self._state.snapshot()

    async def __aenter__(self) -> MarketSubscription:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()


class MarketEventBus:
    """Ordered in-process event bus with explicit per-subscriber backpressure."""

    def __init__(self, *, dedupe_capacity: int = 50_000) -> None:
        if dedupe_capacity <= 0:
            raise ValueError("dedupe_capacity must be positive")
        self.dedupe_capacity = dedupe_capacity
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._subscribers: dict[str, _SubscriberState] = {}
        self._publish_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._closed = False
        self._sequence = 0
        self._published = 0
        self._duplicates = 0
        self._delivered = 0
        self._dropped_oldest = 0
        self._disconnected = 0
        self._blocked = 0
        self._publish_failures = 0
        self._last_publish_ms: int | None = None
        self._last_latency_ms: int | None = None
        self._max_latency_ms = 0

    @property
    def closed(self) -> bool:
        return self._closed

    async def subscribe(
        self,
        name: str,
        *,
        event_filter: MarketEventFilter | None = None,
        capacity: int = 1024,
        policy: BackpressurePolicy = BackpressurePolicy.BLOCK,
    ) -> MarketSubscription:
        if not name:
            raise ValueError("subscription name must not be empty")
        if capacity <= 0:
            raise ValueError("subscription capacity must be positive")
        state = _SubscriberState(
            name,
            event_filter or MarketEventFilter(),
            capacity,
            policy,
        )
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("market event bus is closed")
            if name in self._subscribers:
                raise ValueError(f"subscription already exists: {name}")
            self._subscribers[name] = state
        return MarketSubscription(self, state)

    async def unsubscribe(self, name: str) -> bool:
        async with self._state_lock:
            state = self._subscribers.pop(name, None)
        if state is None:
            return False
        await state.close(clear=True)
        return True

    async def publish(self, event: MarketEvent) -> PublishResult:
        async with self._publish_lock:
            if self._closed:
                raise RuntimeError("market event bus is closed")
            if event.sequence != 0:
                raise ValueError("only unsequenced market events may be published")
            if event.event_id in self._seen:
                self._seen.move_to_end(event.event_id)
                self._duplicates += 1
                return PublishResult(None, True, 0, 0, 0, 0, 0)

            self._sequence += 1
            published = event.with_sequence(self._sequence)
            async with self._state_lock:
                subscribers = tuple(self._subscribers.values())

            outcomes: list[_DeliveryOutcome] = []
            try:
                for subscriber in subscribers:
                    outcomes.append(await subscriber.enqueue(published))
            except BaseException:
                self._publish_failures += 1
                raise

            disconnected_names = {
                subscriber.name
                for subscriber, outcome in zip(subscribers, outcomes, strict=True)
                if outcome.disconnected
            }
            if disconnected_names:
                async with self._state_lock:
                    for name in disconnected_names:
                        current = self._subscribers.get(name)
                        if current is not None and current.closed:
                            self._subscribers.pop(name, None)

            self._seen[event.event_id] = None
            while len(self._seen) > self.dedupe_capacity:
                self._seen.popitem(last=False)

            matched = sum(outcome.matched for outcome in outcomes)
            delivered = sum(outcome.delivered for outcome in outcomes)
            dropped = sum(outcome.dropped_oldest for outcome in outcomes)
            disconnected = sum(outcome.disconnected for outcome in outcomes)
            blocked = sum(outcome.blocked for outcome in outcomes)
            self._published += 1
            self._delivered += delivered
            self._dropped_oldest += dropped
            self._disconnected += disconnected
            self._blocked += blocked
            self._last_publish_ms = int(time.time() * 1000)
            self._last_latency_ms = published.latency_ms
            self._max_latency_ms = max(self._max_latency_ms, published.latency_ms)
            return PublishResult(
                published,
                False,
                matched,
                delivered,
                dropped,
                disconnected,
                blocked,
            )

    async def close(self) -> None:
        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            subscribers = tuple(self._subscribers.values())
            self._subscribers.clear()
        for subscriber in subscribers:
            await subscriber.close(clear=False)

    async def health_snapshot(self) -> dict[str, Any]:
        async with self._state_lock:
            subscribers = tuple(self._subscribers.values())
        subscriber_snapshots = [await subscriber.snapshot() for subscriber in subscribers]
        return {
            "closed": self._closed,
            "last_sequence": self._sequence,
            "published": self._published,
            "duplicates": self._duplicates,
            "delivered": self._delivered,
            "dropped_oldest": self._dropped_oldest,
            "disconnected": self._disconnected,
            "blocked_subscribers": self._blocked,
            "publish_failures": self._publish_failures,
            "last_publish_ms": self._last_publish_ms,
            "last_latency_ms": self._last_latency_ms,
            "max_latency_ms": self._max_latency_ms,
            "dedupe_size": len(self._seen),
            "dedupe_capacity": self.dedupe_capacity,
            "active_subscribers": len(subscriber_snapshots),
            "subscribers": subscriber_snapshots,
        }
