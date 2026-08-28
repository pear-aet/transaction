from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from .market_bus import BackpressurePolicy
from .risk_contracts import (
    RiskDecision,
    RiskDecisionFilter,
    RiskDecisionStatus,
)


@dataclass(frozen=True, slots=True)
class RiskPublishResult:
    decision: RiskDecision | None
    duplicate: bool
    matched_subscribers: int
    delivered: int
    critical_matched: int
    critical_delivered: int
    dropped_oldest: int
    disconnected: int
    blocked_subscribers: int


@dataclass(frozen=True, slots=True)
class _DeliveryOutcome:
    matched: bool = False
    delivered: bool = False
    critical: bool = False
    dropped_oldest: bool = False
    disconnected: bool = False
    blocked: bool = False


class _SubscriberState:
    def __init__(
        self,
        name: str,
        decision_filter: RiskDecisionFilter,
        capacity: int,
        policy: BackpressurePolicy,
        critical: bool,
    ) -> None:
        self.name = name
        self.decision_filter = decision_filter
        self.capacity = capacity
        self.policy = policy
        self.critical = critical
        self._decisions: deque[RiskDecision] = deque()
        self._condition = asyncio.Condition()
        self.closed = False
        self.delivered = 0
        self.consumed = 0
        self.dropped_oldest = 0
        self.blocked_count = 0
        self.last_enqueued_sequence = 0
        self.last_consumed_sequence = 0

    def matches(self, decision: RiskDecision) -> bool:
        return not self.closed and self.decision_filter.matches(decision)

    async def enqueue(self, decision: RiskDecision) -> _DeliveryOutcome:
        if not self.decision_filter.matches(decision):
            return _DeliveryOutcome()
        async with self._condition:
            if self.closed:
                return _DeliveryOutcome(matched=True, critical=self.critical, disconnected=True)
            blocked = False
            dropped = False
            if self.policy is BackpressurePolicy.BLOCK:
                if len(self._decisions) >= self.capacity:
                    blocked = True
                    self.blocked_count += 1
                while len(self._decisions) >= self.capacity and not self.closed:
                    await self._condition.wait()
                if self.closed:
                    return _DeliveryOutcome(
                        matched=True,
                        critical=self.critical,
                        disconnected=True,
                        blocked=blocked,
                    )
            elif len(self._decisions) >= self.capacity:
                if self.policy is BackpressurePolicy.DROP_OLDEST:
                    self._decisions.popleft()
                    self.dropped_oldest += 1
                    dropped = True
                else:
                    self.closed = True
                    self._condition.notify_all()
                    return _DeliveryOutcome(
                        matched=True,
                        critical=self.critical,
                        disconnected=True,
                    )

            self._decisions.append(decision)
            self.delivered += 1
            self.last_enqueued_sequence = decision.sequence
            self._condition.notify_all()
            return _DeliveryOutcome(
                matched=True,
                delivered=True,
                critical=self.critical,
                dropped_oldest=dropped,
                blocked=blocked,
            )

    async def get(self) -> RiskDecision:
        async with self._condition:
            while not self._decisions and not self.closed:
                await self._condition.wait()
            if not self._decisions:
                raise StopAsyncIteration
            decision = self._decisions.popleft()
            self.consumed += 1
            self.last_consumed_sequence = decision.sequence
            self._condition.notify_all()
            return decision

    async def close(self, *, clear: bool) -> None:
        async with self._condition:
            self.closed = True
            if clear:
                self._decisions.clear()
            self._condition.notify_all()

    async def snapshot(self) -> dict[str, Any]:
        async with self._condition:
            return {
                "name": self.name,
                "critical": self.critical,
                "policy": self.policy.value,
                "capacity": self.capacity,
                "queue_depth": len(self._decisions),
                "closed": self.closed,
                "delivered": self.delivered,
                "consumed": self.consumed,
                "dropped_oldest": self.dropped_oldest,
                "blocked_count": self.blocked_count,
                "last_enqueued_sequence": self.last_enqueued_sequence,
                "last_consumed_sequence": self.last_consumed_sequence,
            }


class RiskDecisionSubscription:
    def __init__(self, bus: RiskDecisionBus, state: _SubscriberState) -> None:
        self._bus = bus
        self._state = state

    @property
    def name(self) -> str:
        return self._state.name

    @property
    def critical(self) -> bool:
        return self._state.critical

    @property
    def closed(self) -> bool:
        return self._state.closed

    def __aiter__(self) -> RiskDecisionSubscription:
        return self

    async def __anext__(self) -> RiskDecision:
        return await self._state.get()

    async def get(self, timeout: float | None = None) -> RiskDecision:
        if timeout is None:
            return await self._state.get()
        return await asyncio.wait_for(self._state.get(), timeout=timeout)

    async def close(self) -> None:
        await self._bus.unsubscribe(self.name)

    async def snapshot(self) -> dict[str, Any]:
        return await self._state.snapshot()

    async def __aenter__(self) -> RiskDecisionSubscription:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()


class RiskDecisionBus:
    """Ordered fan-out bus for persisted, unexpired approved risk decisions."""

    def __init__(
        self,
        *,
        dedupe_capacity: int = 50_000,
        require_critical_delivery: bool = True,
    ) -> None:
        if dedupe_capacity <= 0:
            raise ValueError("dedupe_capacity must be positive")
        self.dedupe_capacity = dedupe_capacity
        self.require_critical_delivery = require_critical_delivery
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._subscribers: dict[str, _SubscriberState] = {}
        self._publish_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._closed = False
        self._sequence = 0
        self._published = 0
        self._duplicates = 0
        self._rejected_no_critical = 0
        self._delivered = 0
        self._critical_delivered = 0
        self._dropped_oldest = 0
        self._disconnected = 0
        self._blocked = 0
        self._publish_failures = 0
        self._last_publish_ms: int | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    async def subscribe(
        self,
        name: str,
        *,
        decision_filter: RiskDecisionFilter | None = None,
        capacity: int = 1024,
        policy: BackpressurePolicy = BackpressurePolicy.BLOCK,
        critical: bool = True,
    ) -> RiskDecisionSubscription:
        if not name:
            raise ValueError("subscription name must not be empty")
        if capacity <= 0:
            raise ValueError("subscription capacity must be positive")
        if critical and policy is not BackpressurePolicy.BLOCK:
            raise ValueError("critical risk subscribers must use blocking backpressure")
        state = _SubscriberState(
            name,
            decision_filter or RiskDecisionFilter(),
            capacity,
            policy,
            critical,
        )
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("risk decision bus is closed")
            if name in self._subscribers:
                raise ValueError(f"subscription already exists: {name}")
            self._subscribers[name] = state
        return RiskDecisionSubscription(self, state)

    async def unsubscribe(self, name: str) -> bool:
        async with self._state_lock:
            state = self._subscribers.pop(name, None)
        if state is None:
            return False
        await state.close(clear=True)
        return True

    async def publish(
        self,
        decision: RiskDecision,
        *,
        now_ms: int | None = None,
    ) -> RiskPublishResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        async with self._publish_lock:
            if self._closed:
                raise RuntimeError("risk decision bus is closed")
            if decision.status is not RiskDecisionStatus.APPROVED:
                raise ValueError("only approved risk decisions may be published")
            if decision.storage_sequence is None:
                raise ValueError("risk decision must be persisted before publication")
            if current > decision.valid_until:
                raise ValueError("expired risk decision cannot be published")
            if decision.sequence != 0:
                raise ValueError("only unsequenced risk decisions may be published")
            if decision.decision_id in self._seen:
                self._seen.move_to_end(decision.decision_id)
                self._duplicates += 1
                return RiskPublishResult(None, True, 0, 0, 0, 0, 0, 0, 0)

            async with self._state_lock:
                subscribers = tuple(self._subscribers.values())
            critical_matched = sum(
                subscriber.critical and subscriber.matches(decision) for subscriber in subscribers
            )
            if self.require_critical_delivery and critical_matched == 0:
                self._rejected_no_critical += 1
                return RiskPublishResult(None, False, 0, 0, 0, 0, 0, 0, 0)

            self._sequence += 1
            published = decision.with_sequence(self._sequence)
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

            matched = sum(outcome.matched for outcome in outcomes)
            delivered = sum(outcome.delivered for outcome in outcomes)
            critical_delivered = sum(outcome.delivered and outcome.critical for outcome in outcomes)
            dropped = sum(outcome.dropped_oldest for outcome in outcomes)
            disconnected = sum(outcome.disconnected for outcome in outcomes)
            blocked = sum(outcome.blocked for outcome in outcomes)
            if self.require_critical_delivery and critical_delivered == 0:
                self._publish_failures += 1
                raise RuntimeError("approved risk decision reached no critical subscriber")

            self._seen[decision.decision_id] = None
            while len(self._seen) > self.dedupe_capacity:
                self._seen.popitem(last=False)
            self._published += 1
            self._delivered += delivered
            self._critical_delivered += critical_delivered
            self._dropped_oldest += dropped
            self._disconnected += disconnected
            self._blocked += blocked
            self._last_publish_ms = current
            return RiskPublishResult(
                published,
                False,
                matched,
                delivered,
                critical_matched,
                critical_delivered,
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
        snapshots = [await subscriber.snapshot() for subscriber in subscribers]
        return {
            "closed": self._closed,
            "last_sequence": self._sequence,
            "published": self._published,
            "duplicates": self._duplicates,
            "rejected_no_critical": self._rejected_no_critical,
            "delivered": self._delivered,
            "critical_delivered": self._critical_delivered,
            "dropped_oldest": self._dropped_oldest,
            "disconnected": self._disconnected,
            "blocked_subscribers": self._blocked,
            "publish_failures": self._publish_failures,
            "last_publish_ms": self._last_publish_ms,
            "dedupe_size": len(self._seen),
            "dedupe_capacity": self.dedupe_capacity,
            "active_subscribers": len(snapshots),
            "critical_subscribers": sum(snapshot["critical"] for snapshot in snapshots),
            "subscribers": snapshots,
        }
