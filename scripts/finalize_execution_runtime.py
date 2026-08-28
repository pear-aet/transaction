from pathlib import Path


ROOT = Path(".")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


engine_path = ROOT / "src/crypto_quant/execution_engine.py"
engine_text = engine_path.read_text(encoding="utf-8")
if "self._outbox_lock = asyncio.Lock()" not in engine_text:
    marker = "        self._processing_lock = asyncio.Lock()\n"
    if engine_text.count(marker) != 1:
        raise SystemExit("cannot locate ExecutionEngine processing lock")
    engine_text = engine_text.replace(
        marker,
        marker + "        self._outbox_lock = asyncio.Lock()\n",
        1,
    )

if "async def _flush_outbox_unlocked(" not in engine_text:
    start = engine_text.index("    async def flush_outbox(")
    end = engine_text.index("    async def bootstrap(", start)
    original = engine_text[start:end]
    renamed = original.replace(
        "    async def flush_outbox(",
        "    async def _flush_outbox_unlocked(",
        1,
    )
    wrapper = '''    async def flush_outbox(
        self,
        *,
        now_ms: int | None = None,
    ) -> ExecutionBootstrapResult:
        async with self._outbox_lock:
            return await self._flush_outbox_unlocked(now_ms=now_ms)

'''
    engine_text = engine_text[:start] + renamed + wrapper + engine_text[end:]
engine_path.write_text(engine_text, encoding="utf-8")


write(
    "src/crypto_quant/execution_runtime.py",
    '''from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .execution_engine import ExecutionDispatchError, ExecutionEngine
from .market_bus import BackpressurePolicy
from .risk_bus import RiskDecisionBus, RiskDecisionSubscription
from .risk_contracts import RiskDecisionFilter, RiskDecisionStatus
from .risk_replay import DurableRiskFilter, RiskReplayLedger
from .risk_store import RiskStore


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeSettings:
    subscription_name: str
    durable_consumer_name: str
    risk_engine_instance_ids: frozenset[str] | None = None
    account_ids: frozenset[str] | None = None
    symbols: frozenset[str] | None = None
    capacity: int = 1024
    replay_batch_size: int = 1

    def __post_init__(self) -> None:
        if not self.subscription_name or not self.durable_consumer_name:
            raise ValueError("execution runtime consumer names must not be empty")
        if self.capacity <= 0:
            raise ValueError("execution runtime capacity must be positive")
        if self.replay_batch_size != 1:
            raise ValueError(
                "execution runtime replay_batch_size must remain one for atomic recovery"
            )

    def decision_filter(self) -> RiskDecisionFilter:
        return RiskDecisionFilter(
            engine_instance_ids=self.risk_engine_instance_ids,
            account_ids=self.account_ids,
            symbols=self.symbols,
            statuses=frozenset({RiskDecisionStatus.APPROVED}),
        )


@dataclass(slots=True)
class ExecutionRuntimeHealth:
    running: bool = False
    started_at_ms: int | None = None
    stopped_at_ms: int | None = None
    bootstrap_runs: int = 0
    replayed_decisions: int = 0
    replay_skipped: int = 0
    live_decisions: int = 0
    duplicate_live_decisions: int = 0
    failures: int = 0
    last_decision_id: str | None = None
    last_command_id: str | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at_ms": self.started_at_ms,
            "stopped_at_ms": self.stopped_at_ms,
            "bootstrap_runs": self.bootstrap_runs,
            "replayed_decisions": self.replayed_decisions,
            "replay_skipped": self.replay_skipped,
            "live_decisions": self.live_decisions,
            "duplicate_live_decisions": self.duplicate_live_decisions,
            "failures": self.failures,
            "last_decision_id": self.last_decision_id,
            "last_command_id": self.last_command_id,
            "last_error": self.last_error,
        }


Clock = Callable[[], int]


class ExecutionRuntime:
    """Critical live RiskDecision consumer with durable startup catch-up."""

    def __init__(
        self,
        decision_bus: RiskDecisionBus,
        risk_store: RiskStore,
        execution_engine: ExecutionEngine,
        settings: ExecutionRuntimeSettings,
        *,
        replay_ledger: RiskReplayLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.decision_bus = decision_bus
        self.risk_store = risk_store
        self.execution_engine = execution_engine
        self.settings = settings
        self.replay_ledger = replay_ledger or RiskReplayLedger(risk_store)
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.health = ExecutionRuntimeHealth()
        self._subscription: RiskDecisionSubscription | None = None

    def _durable_filter(self) -> DurableRiskFilter:
        return DurableRiskFilter(decision_filter=self.settings.decision_filter())

    def _record_failure(self, exc: Exception) -> None:
        self.health.failures += 1
        self.health.last_error = f"{exc.__class__.__name__}: {exc}"

    async def _commit_batch(self, batch, now_ms: int) -> None:
        await asyncio.to_thread(
            self.replay_ledger.commit_batch,
            batch,
            updated_at=now_ms,
        )

    async def _catch_up(self) -> None:
        selected_filter = self._durable_filter()
        while True:
            batch = await asyncio.to_thread(
                self.replay_ledger.read_batch,
                self.settings.durable_consumer_name,
                selected_filter,
                limit=self.settings.replay_batch_size,
            )
            if batch.scanned_through == batch.start_sequence and not batch.decisions:
                return
            self.health.replay_skipped += batch.skipped
            if not batch.decisions:
                await self._commit_batch(batch, self.clock())
            else:
                stored = batch.decisions[0]
                try:
                    result = await self.execution_engine.plan_decision(
                        stored.decision,
                        now_ms=self.clock(),
                    )
                except ExecutionDispatchError:
                    persisted = await asyncio.to_thread(
                        self.execution_engine.store.load_by_source_decision,
                        self.execution_engine.settings.engine_instance_id,
                        stored.decision.decision_id,
                    )
                    if persisted is not None:
                        await self._commit_batch(batch, self.clock())
                    raise
                await self._commit_batch(batch, self.clock())
                self.health.replayed_decisions += 1
                self.health.last_decision_id = stored.decision.decision_id
                self.health.last_command_id = result.command.command_id
            if batch.scanned_through >= batch.high_watermark:
                return

    async def bootstrap(self) -> None:
        if self._subscription is None:
            self._subscription = await self.decision_bus.subscribe(
                self.settings.subscription_name,
                decision_filter=self.settings.decision_filter(),
                capacity=self.settings.capacity,
                policy=BackpressurePolicy.BLOCK,
                critical=True,
            )
        await self.execution_engine.bootstrap(now_ms=self.clock())
        await self._catch_up()
        self.health.bootstrap_runs += 1
        self.health.last_error = None

    async def run(self) -> None:
        if self.health.running:
            raise RuntimeError("execution runtime is already running")
        self.health.running = True
        self.health.started_at_ms = self.clock()
        self.health.stopped_at_ms = None
        self.health.last_error = None
        try:
            await self.bootstrap()
            if self._subscription is None:
                raise RuntimeError("execution runtime bootstrap created no subscription")
            async for decision in self._subscription:
                self.health.last_decision_id = decision.decision_id
                result = await self.execution_engine.plan_decision(
                    decision,
                    now_ms=self.clock(),
                )
                self.health.last_command_id = result.command.command_id
                self.health.live_decisions += 1
                if result.duplicate:
                    self.health.duplicate_live_decisions += 1
        except Exception as exc:
            self._record_failure(exc)
            raise
        finally:
            self.health.running = False
            self.health.stopped_at_ms = self.clock()

    async def stop(self) -> None:
        if self._subscription is not None:
            await self._subscription.close()
            self._subscription = None

    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "runtime": self.health.snapshot(),
            "execution_engine": await self.execution_engine.health_snapshot(),
            "upstream_replay": await asyncio.to_thread(self.replay_ledger.metrics),
        }

    async def close(self) -> None:
        await self.stop()
''',
)


COMMON_TEST_HELPERS = '''import asyncio

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution import build_execution_command
from crypto_quant.execution_broker import ExecutionBroker
from crypto_quant.execution_contracts import (
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    OrderState,
    OrderUpdate,
)
from crypto_quant.execution_engine import ExecutionEngine, ExecutionEngineSettings
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
CLOCK = 901_000


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


def make_decision(index: int):
    decision_time = 900_000 + index * 100
    intent = StrategyIntent(
        intent_id=f"execution-intent-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=decision_time,
        created_at=decision_time,
        reference_price=100,
        stop_price=99,
        reason="execution_runtime_test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=index,
    )
    account = AccountRiskSnapshot(
        f"account-{index}",
        "paper",
        decision_time,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    market = MarketRiskSnapshot(
        f"risk-market-{index}",
        SYMBOL,
        decision_time,
        100,
        rules(),
    )
    decision = evaluate_risk(
        RiskContext(
            "risk-primary",
            intent,
            account,
            market,
            RiskPolicy(),
            decision_time + 1,
        )
    )
    assert decision.status is RiskDecisionStatus.APPROVED
    return decision


def persist_decision(store: RiskStore, index: int, *, published: bool):
    decision = make_decision(index)
    commit = store.commit_evaluation(decision, updated_at=CLOCK)
    stored = commit.stored_decision.decision
    if published:
        store.mark_published(stored.decision_id, CLOCK)
    return stored


def execution_market(decision, now_ms: int) -> ExecutionMarketSnapshot:
    return ExecutionMarketSnapshot(
        f"execution-market-{decision.decision_id}",
        decision.symbol,
        now_ms,
        100,
        99.9,
        100.1,
    )


class CountingBroker(ExecutionBroker):
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(self, command, orders, now_ms):
        self.calls += 1
        return tuple(
            OrderUpdate(
                update_id=f"dispatch:{order.order_id}",
                order_id=order.order_id,
                event_time=now_ms,
                new_state=OrderState.DISPATCHED,
                cumulative_filled_quantity=0,
                cumulative_quote_value=0,
                cumulative_fee=0,
                evidence={"broker": "counting"},
            )
            for order in orders
        )
'''

write(
    "tests/test_execution_engine_outbox.py",
    COMMON_TEST_HELPERS
    + '''

def test_unpublished_risk_source_blocks_simulation_dispatch(tmp_path) -> None:
    async def scenario() -> None:
        database = tmp_path / "quant.db"
        risk_store = RiskStore(database)
        decision = persist_decision(risk_store, 1, published=False)
        execution_store = ExecutionStore(database)
        broker = CountingBroker()
        engine = ExecutionEngine(
            ExecutionEngineSettings("execution-primary", "paper"),
            ExecutionPolicy(mode=ExecutionMode.SIMULATION),
            execution_market,
            store=execution_store,
            risk_store=risk_store,
            broker=broker,
        )

        planned = await engine.plan_decision(decision, now_ms=CLOCK)
        assert planned.delivery_state is ExecutionDeliveryState.PENDING
        assert broker.calls == 0
        assert execution_store.metrics()["pending_commands"] == 1

        risk_store.mark_published(decision.decision_id, CLOCK + 1)
        flushed = await engine.flush_outbox(now_ms=CLOCK + 1)
        assert flushed.dispatched == 1
        assert flushed.blocked_on_unpublished_source == 0
        assert broker.calls == 1
        stored = execution_store.load_by_source_decision(
            "execution-primary",
            decision.decision_id,
        )
        assert stored is not None
        assert stored.delivery_state is ExecutionDeliveryState.DISPATCHED

    asyncio.run(scenario())


def test_concurrent_outbox_flush_dispatches_each_command_once(tmp_path) -> None:
    async def scenario() -> None:
        database = tmp_path / "quant.db"
        risk_store = RiskStore(database)
        decision = persist_decision(risk_store, 1, published=True)
        execution_store = ExecutionStore(database)
        policy = ExecutionPolicy(mode=ExecutionMode.SIMULATION)
        command = build_execution_command(
            ExecutionContext(
                "execution-primary",
                decision,
                execution_market(decision, CLOCK),
                policy,
                CLOCK,
            )
        )
        execution_store.commit_command(command, updated_at=CLOCK)
        broker = CountingBroker()
        engine = ExecutionEngine(
            ExecutionEngineSettings("execution-primary", "paper"),
            policy,
            execution_market,
            store=execution_store,
            risk_store=risk_store,
            broker=broker,
        )

        first, second = await asyncio.gather(
            engine.flush_outbox(now_ms=CLOCK),
            engine.flush_outbox(now_ms=CLOCK),
        )
        assert first.dispatched + second.dispatched == 1
        assert broker.calls == 1
        assert execution_store.metrics()["pending_commands"] == 0
        assert execution_store.metrics()["dispatched_commands"] == 1

    asyncio.run(scenario())
''',
)


write(
    "tests/test_execution_runtime.py",
    COMMON_TEST_HELPERS
    + '''
from crypto_quant.execution_runtime import ExecutionRuntime, ExecutionRuntimeSettings
from crypto_quant.risk_bus import RiskDecisionBus


async def wait_for_bootstrap(runtime: ExecutionRuntime) -> None:
    for _ in range(500):
        if runtime.health.bootstrap_runs:
            return
        await asyncio.sleep(0.002)
    raise AssertionError("execution runtime did not complete bootstrap")


def test_runtime_catches_up_durable_decisions_then_processes_live_bus(tmp_path) -> None:
    async def scenario() -> None:
        database = tmp_path / "quant.db"
        risk_store = RiskStore(database)
        first = persist_decision(risk_store, 1, published=True)
        execution_store = ExecutionStore(database)
        decision_bus = RiskDecisionBus()
        engine = ExecutionEngine(
            ExecutionEngineSettings("execution-primary", "paper"),
            ExecutionPolicy(mode=ExecutionMode.SHADOW),
            execution_market,
            store=execution_store,
            risk_store=risk_store,
        )
        runtime = ExecutionRuntime(
            decision_bus,
            risk_store,
            engine,
            ExecutionRuntimeSettings("execution-live", "execution-upstream-v1"),
            clock=lambda: CLOCK,
        )
        task = asyncio.create_task(runtime.run())
        await wait_for_bootstrap(runtime)

        recovered = execution_store.load_by_source_decision(
            "execution-primary",
            first.decision_id,
        )
        assert recovered is not None
        assert recovered.delivery_state is ExecutionDeliveryState.RECORDED
        assert runtime.health.replayed_decisions == 1
        assert (
            runtime.replay_ledger.offset(
                "execution-upstream-v1",
                runtime._durable_filter(),
            ).sequence
            == first.storage_sequence
        )

        second = persist_decision(risk_store, 2, published=True)
        published = await decision_bus.publish(second, now_ms=CLOCK)
        assert published.critical_delivered == 1
        for _ in range(500):
            stored = execution_store.load_by_source_decision(
                "execution-primary",
                second.decision_id,
            )
            if stored is not None and runtime.health.live_decisions == 1:
                break
            await asyncio.sleep(0.002)
        stored = execution_store.load_by_source_decision(
            "execution-primary",
            second.decision_id,
        )
        assert stored is not None
        assert stored.delivery_state is ExecutionDeliveryState.RECORDED
        assert runtime.health.live_decisions == 1

        await runtime.stop()
        await asyncio.wait_for(task, timeout=1.0)
        await decision_bus.close()

    asyncio.run(scenario())
''',
)
