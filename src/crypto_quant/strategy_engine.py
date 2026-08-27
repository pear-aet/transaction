from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .config import StrategyConfig
from .data import timeframe_ms, validate_candles
from .domain import Candle
from .market_events import (
    MarketEvent,
    MarketEventKind,
    candle_from_market_event,
    candle_market_event,
)
from .storage import Repository
from .strategy import (
    StrategyContext,
    StrategyDecision,
    evaluate_strategy,
    strategy_spec,
)
from .strategy_bus import StrategyIntentBus
from .strategy_contracts import (
    StrategyIntent,
    strategy_intent_from_signal,
    strategy_parameter_hash,
)
from .strategy_store import (
    EvaluationCommit,
    StrategyCheckpoint,
    StrategyCheckpointError,
    StrategyDeliveryState,
    StrategyEvaluationMode,
    StrategyStore,
)


class StrategyBarrierError(RuntimeError):
    """Raised when a low-timeframe decision cannot obtain its required HTF close."""


class StrategyReplayRequired(RuntimeError):
    """Raised when stored market history advanced beyond the strategy checkpoint."""


class StrategyDeliveryError(RuntimeError):
    """Raised when a persisted intent has no critical downstream subscriber."""


class RecoveryMode(StrEnum):
    FAIL = "fail"
    REPLAY = "replay"
    RESET = "reset"


PositionSideValue = str | None
PositionSideProvider = Callable[
    [str, int],
    PositionSideValue | Awaitable[PositionSideValue],
]
DecisionFunction = Callable[[StrategyContext], StrategyDecision]


@dataclass(frozen=True, slots=True)
class StrategyEngineSettings:
    instance_id: str
    low_window: int = 1000
    high_window: int = 500
    max_pending_low_events: int = 16
    outbox_batch_size: int = 1000
    initial_freshness_intervals: int = 2
    require_market_sequence: bool = True
    require_intent_subscriber: bool = True

    def __post_init__(self) -> None:
        if not self.instance_id:
            raise ValueError("strategy instance_id must not be empty")
        if self.low_window < 50 or self.high_window < 20:
            raise ValueError("strategy history windows are below minimum warmup")
        if self.max_pending_low_events <= 0:
            raise ValueError("max_pending_low_events must be positive")
        if not 1 <= self.outbox_batch_size <= 10_000:
            raise ValueError("outbox_batch_size must be in [1, 10000]")
        if self.initial_freshness_intervals < 0:
            raise ValueError("initial_freshness_intervals must be non-negative")


@dataclass(frozen=True, slots=True)
class SymbolBootstrap:
    symbol: str
    low_candles: int
    high_candles: int
    checkpoint_before: int | None
    checkpoint_after: int
    missed_events: int
    replayed_events: int
    reset_events: int
    initial_evaluation: bool


@dataclass(frozen=True, slots=True)
class StrategyBootstrapResult:
    symbols: tuple[SymbolBootstrap, ...]
    recovered_intents: int


@dataclass(slots=True)
class StrategyEngineHealth:
    bootstrapped: bool = False
    bootstrap_runs: int = 0
    received_events: int = 0
    ignored_events: int = 0
    duplicate_events: int = 0
    high_events: int = 0
    low_events: int = 0
    barrier_waits: int = 0
    warmup_decisions: int = 0
    evaluations: int = 0
    initial_evaluations: int = 0
    signals: int = 0
    intents_persisted: int = 0
    intents_published: int = 0
    intents_suppressed: int = 0
    outbox_recoveries: int = 0
    replayed_events: int = 0
    reset_events: int = 0
    failures: int = 0
    last_market_event_time: int | None = None
    last_decision_time: int | None = None
    last_intent_id: str | None = None
    last_error: str | None = None

    def snapshot(self, pending: dict[str, int]) -> dict[str, Any]:
        return {
            "bootstrapped": self.bootstrapped,
            "bootstrap_runs": self.bootstrap_runs,
            "received_events": self.received_events,
            "ignored_events": self.ignored_events,
            "duplicate_events": self.duplicate_events,
            "high_events": self.high_events,
            "low_events": self.low_events,
            "barrier_waits": self.barrier_waits,
            "warmup_decisions": self.warmup_decisions,
            "evaluations": self.evaluations,
            "initial_evaluations": self.initial_evaluations,
            "signals": self.signals,
            "intents_persisted": self.intents_persisted,
            "intents_published": self.intents_published,
            "intents_suppressed": self.intents_suppressed,
            "outbox_recoveries": self.outbox_recoveries,
            "replayed_events": self.replayed_events,
            "reset_events": self.reset_events,
            "failures": self.failures,
            "last_market_event_time": self.last_market_event_time,
            "last_decision_time": self.last_decision_time,
            "last_intent_id": self.last_intent_id,
            "last_error": self.last_error,
            "pending_low_events": pending,
        }


@dataclass(slots=True)
class _SymbolState:
    low: list[Candle] = field(default_factory=list)
    high: list[Candle] = field(default_factory=list)
    pending_low: dict[int, MarketEvent] = field(default_factory=dict)


class StrategyEngine:
    """Closed-bar strategy evaluator shared by live consumers and research adapters."""

    def __init__(
        self,
        repository: Repository,
        configs: dict[str, StrategyConfig],
        settings: StrategyEngineSettings,
        position_side_provider: PositionSideProvider,
        *,
        store: StrategyStore | None = None,
        intent_bus: StrategyIntentBus | None = None,
        decision_function: DecisionFunction = evaluate_strategy,
    ) -> None:
        if not configs:
            raise ValueError("strategy engine requires at least one symbol config")
        if position_side_provider is None:
            raise ValueError("position_side_provider is required")
        for symbol, config in configs.items():
            if symbol != config.symbol:
                raise ValueError("strategy config key must match config.symbol")
            high_ms = timeframe_ms(config.high_timeframe)
            low_ms = timeframe_ms(config.low_timeframe)
            if high_ms <= low_ms or high_ms % low_ms:
                raise ValueError(
                    f"invalid timeframe hierarchy for {symbol}: "
                    f"{config.high_timeframe}/{config.low_timeframe}"
                )
            spec = strategy_spec(config.entry_model)
            if settings.low_window < max(spec.minimum_low_bars, config.entry_lookback + 2):
                raise ValueError(f"low history window is too small for {symbol}")
            if settings.high_window < spec.minimum_high_bars:
                raise ValueError(f"high history window is too small for {symbol}")
        self.repository = repository
        self.configs = dict(configs)
        self.settings = settings
        self.position_side_provider = position_side_provider
        self.store = store or StrategyStore(repository.path)
        self.intent_bus = intent_bus or StrategyIntentBus(
            require_critical_delivery=settings.require_intent_subscriber
        )
        self.decision_function = decision_function
        self.health = StrategyEngineHealth()
        self._states = {symbol: _SymbolState() for symbol in configs}
        self._processing_lock = asyncio.Lock()

    @staticmethod
    def _append_history(history: list[Candle], candle: Candle, limit: int) -> bool:
        if history and candle.timestamp < history[-1].timestamp:
            existing = next((item for item in history if item.timestamp == candle.timestamp), None)
            if existing == candle:
                return False
            raise RuntimeError(f"strategy candle history moved backwards at {candle.timestamp}")
        if history and candle.timestamp == history[-1].timestamp:
            if history[-1] != candle:
                raise RuntimeError(f"strategy candle revision at {candle.timestamp}")
            return False
        history.append(candle)
        if len(history) > limit:
            del history[: len(history) - limit]
        return True

    @staticmethod
    def _closed_history(
        history: list[Candle],
        timeframe: str,
        decision_time: int,
    ) -> list[Candle]:
        interval = timeframe_ms(timeframe)
        return [item for item in history if item.timestamp + interval <= decision_time]

    @staticmethod
    def _event_from_stored_candle(
        symbol: str,
        timeframe: str,
        candle: Candle,
    ) -> MarketEvent:
        close_time = candle.timestamp + timeframe_ms(timeframe)
        return candle_market_event(
            symbol,
            timeframe,
            candle,
            source_time=close_time,
            received_at=close_time,
        )

    def _strategy_signature(self, symbol: str) -> tuple[str, str, str, str, str]:
        config = self.configs[symbol]
        spec = strategy_spec(config.entry_model)
        return (
            spec.strategy_id,
            spec.version,
            strategy_parameter_hash(config),
            config.high_timeframe,
            config.low_timeframe,
        )

    def _checkpoint(self, symbol: str) -> StrategyCheckpoint | None:
        return self.store.load_checkpoint(self.settings.instance_id, symbol)

    def _validate_checkpoint(self, symbol: str, checkpoint: StrategyCheckpoint) -> None:
        expected = self._strategy_signature(symbol)
        actual = (
            checkpoint.strategy_id,
            checkpoint.strategy_version,
            checkpoint.parameter_hash,
            checkpoint.high_timeframe,
            checkpoint.low_timeframe,
        )
        if actual != expected:
            raise StrategyCheckpointError(
                f"strategy configuration changed for {self.settings.instance_id} {symbol}; "
                "use a new instance_id"
            )

    def _record_failure(self, exc: Exception) -> None:
        self.health.failures += 1
        self.health.last_error = f"{exc.__class__.__name__}: {exc}"

    async def _position_side(
        self,
        provider: PositionSideProvider,
        symbol: str,
        decision_time: int,
    ) -> PositionSideValue:
        result = await asyncio.to_thread(provider, symbol, decision_time)
        if inspect.isawaitable(result):
            result = await result
        if result not in {None, "long", "short"}:
            raise ValueError(f"invalid position side for {symbol}: {result}")
        return result

    async def _flush_outbox(self) -> int:
        published = 0
        while True:
            pending = await asyncio.to_thread(
                self.store.pending_intents,
                instance_id=self.settings.instance_id,
                limit=self.settings.outbox_batch_size,
            )
            if not pending:
                return published
            for intent in pending:
                result = await self.intent_bus.publish(intent)
                if result.intent is None and not result.duplicate:
                    raise StrategyDeliveryError(
                        f"strategy intent has no matching critical delivery: "
                        f"{intent.intent_id}"
                    )
                await asyncio.to_thread(
                    self.store.mark_published,
                    intent.intent_id,
                    int(time.time() * 1000),
                )
                self.health.intents_published += 1
                self.health.last_intent_id = intent.intent_id
                published += 1
            if len(pending) < self.settings.outbox_batch_size:
                return published

    def _required_high_close(self, symbol: str, decision_time: int) -> int:
        high_ms = timeframe_ms(self.configs[symbol].high_timeframe)
        return (decision_time // high_ms) * high_ms

    def _latest_available_high_close(self, symbol: str, decision_time: int) -> int | None:
        config = self.configs[symbol]
        high_history = self._closed_history(
            self._states[symbol].high,
            config.high_timeframe,
            decision_time,
        )
        if not high_history:
            return None
        return high_history[-1].timestamp + timeframe_ms(config.high_timeframe)

    def _barrier_ready(self, symbol: str, low_event: MarketEvent) -> bool:
        latest = self._latest_available_high_close(symbol, low_event.event_time)
        return latest is not None and latest >= self._required_high_close(
            symbol, low_event.event_time
        )

    def _assert_barrier(self, symbol: str, low_event: MarketEvent) -> None:
        required = self._required_high_close(symbol, low_event.event_time)
        latest = self._latest_available_high_close(symbol, low_event.event_time)
        if latest is None or latest < required:
            raise StrategyBarrierError(
                f"missing required {self.configs[symbol].high_timeframe} close for "
                f"{symbol}: required={required}, latest={latest}"
            )

    async def _commit_without_intent(
        self,
        symbol: str,
        event: MarketEvent,
        mode: StrategyEvaluationMode,
        updated_at: int,
    ) -> EvaluationCommit:
        self._assert_barrier(symbol, event)
        strategy_id, version, parameter_hash, high_timeframe, low_timeframe = (
            self._strategy_signature(symbol)
        )
        return await asyncio.to_thread(
            self.store.commit_evaluation,
            instance_id=self.settings.instance_id,
            symbol=symbol,
            event_time=event.event_time,
            event_id=event.event_id,
            updated_at=updated_at,
            strategy_id=strategy_id,
            strategy_version=version,
            parameter_hash=parameter_hash,
            high_timeframe=high_timeframe,
            low_timeframe=low_timeframe,
            intent=None,
            mode=mode,
            delivery_required=False,
        )

    async def _evaluate_low_event(
        self,
        symbol: str,
        event: MarketEvent,
        *,
        mode: StrategyEvaluationMode = StrategyEvaluationMode.LIVE,
        delivery_required: bool = True,
        suppression_reason: str | None = None,
        position_provider: PositionSideProvider | None = None,
    ) -> EvaluationCommit | None:
        config = self.configs[symbol]
        checkpoint = await asyncio.to_thread(self._checkpoint, symbol)
        if checkpoint is not None:
            self._validate_checkpoint(symbol, checkpoint)
            if event.event_time < checkpoint.last_event_time:
                self.health.duplicate_events += 1
                return None
            if event.event_time == checkpoint.last_event_time:
                if event.event_id != checkpoint.last_event_id:
                    raise RuntimeError(
                        f"strategy checkpoint event changed for {symbol} at {event.event_time}"
                    )
                self.health.duplicate_events += 1
                if delivery_required:
                    await self._flush_outbox()
                return None

        self._assert_barrier(symbol, event)
        state = self._states[symbol]
        low_history = self._closed_history(state.low, config.low_timeframe, event.event_time)
        high_history = self._closed_history(state.high, config.high_timeframe, event.event_time)
        spec = strategy_spec(config.entry_model)
        parameter_hash = strategy_parameter_hash(config)
        minimum_low = max(spec.minimum_low_bars, config.entry_lookback + 2)
        signal_intent: StrategyIntent | None = None
        if len(low_history) < minimum_low or len(high_history) < spec.minimum_high_bars:
            self.health.warmup_decisions += 1
        else:
            provider = position_provider or self.position_side_provider
            position_side = await self._position_side(
                provider,
                symbol,
                event.event_time,
            )
            context = StrategyContext(
                tuple(low_history),
                tuple(high_history),
                config,
                event.event_time,
                position_side,
            )
            decision = await asyncio.to_thread(self.decision_function, context)
            self.health.evaluations += 1
            if decision.signal is not None:
                signal_intent = strategy_intent_from_signal(
                    decision.signal,
                    decision,
                    config,
                    spec,
                    event,
                    instance_id=self.settings.instance_id,
                    created_at=int(time.time() * 1000),
                )
                self.health.signals += 1

        committed = await asyncio.to_thread(
            self.store.commit_evaluation,
            instance_id=self.settings.instance_id,
            symbol=symbol,
            event_time=event.event_time,
            event_id=event.event_id,
            updated_at=int(time.time() * 1000),
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            parameter_hash=parameter_hash,
            high_timeframe=config.high_timeframe,
            low_timeframe=config.low_timeframe,
            intent=signal_intent,
            mode=mode,
            delivery_required=delivery_required,
            suppression_reason=suppression_reason,
        )
        if committed.intent_inserted:
            self.health.intents_persisted += 1
            if (
                committed.stored_intent is not None
                and committed.stored_intent.delivery_state
                is StrategyDeliveryState.SUPPRESSED
            ):
                self.health.intents_suppressed += 1
        if delivery_required:
            await self._flush_outbox()
        self.health.last_decision_time = event.event_time
        return committed

    async def bootstrap(
        self,
        *,
        recovery_mode: RecoveryMode = RecoveryMode.FAIL,
        historical_position_provider: PositionSideProvider | None = None,
        now_ms: int | None = None,
    ) -> StrategyBootstrapResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        async with self._processing_lock:
            try:
                recovered = await self._flush_outbox()
                self.health.outbox_recoveries += recovered
                outputs: list[SymbolBootstrap] = []
                for symbol, config in self.configs.items():
                    low_all, high_all = await asyncio.gather(
                        asyncio.to_thread(
                            self.repository.load_candles,
                            symbol,
                            config.low_timeframe,
                            0,
                            "contract",
                        ),
                        asyncio.to_thread(
                            self.repository.load_candles,
                            symbol,
                            config.high_timeframe,
                            0,
                            "contract",
                        ),
                    )
                    spec = strategy_spec(config.entry_model)
                    minimum_low = max(spec.minimum_low_bars, config.entry_lookback + 2)
                    if len(low_all) < minimum_low or len(high_all) < spec.minimum_high_bars:
                        raise RuntimeError(
                            f"insufficient strategy bootstrap history for {symbol}: "
                            f"low={len(low_all)}/{minimum_low}, "
                            f"high={len(high_all)}/{spec.minimum_high_bars}"
                        )
                    validate_candles(low_all, timeframe_ms(config.low_timeframe))
                    validate_candles(high_all, timeframe_ms(config.high_timeframe))
                    state = self._states[symbol]
                    state.low = list(low_all)
                    state.high = list(high_all)
                    state.pending_low.clear()

                    checkpoint = await asyncio.to_thread(self._checkpoint, symbol)
                    if checkpoint is not None:
                        self._validate_checkpoint(symbol, checkpoint)
                    checkpoint_before = None if checkpoint is None else checkpoint.last_event_time
                    low_events = [
                        self._event_from_stored_candle(symbol, config.low_timeframe, candle)
                        for candle in low_all
                    ]
                    latest = low_events[-1]
                    missed = (
                        []
                        if checkpoint is None
                        else [
                            event
                            for event in low_events
                            if event.event_time > checkpoint.last_event_time
                        ]
                    )
                    replayed = 0
                    reset = 0
                    initial_evaluation = False
                    if checkpoint is None:
                        age = current - latest.event_time
                        if age < 0:
                            raise RuntimeError(
                                f"latest low-timeframe candle closes in the future for {symbol}"
                            )
                        freshness = self.settings.initial_freshness_intervals * timeframe_ms(
                            config.low_timeframe
                        )
                        if age <= freshness:
                            await self._evaluate_low_event(
                                symbol,
                                latest,
                                mode=StrategyEvaluationMode.INITIAL,
                                delivery_required=True,
                            )
                            initial_evaluation = True
                            self.health.initial_evaluations += 1
                        else:
                            await self._commit_without_intent(
                                symbol,
                                latest,
                                StrategyEvaluationMode.BASELINE,
                                current,
                            )
                    elif missed:
                        if recovery_mode is RecoveryMode.FAIL:
                            raise StrategyReplayRequired(
                                f"{symbol} has {len(missed)} unevaluated low-timeframe closes"
                            )
                        if recovery_mode is RecoveryMode.RESET:
                            await self._commit_without_intent(
                                symbol,
                                latest,
                                StrategyEvaluationMode.RESET,
                                current,
                            )
                            reset = len(missed)
                            self.health.reset_events += reset
                        else:
                            if historical_position_provider is None:
                                raise ValueError(
                                    "historical_position_provider is required for replay recovery"
                                )
                            for event in missed:
                                await self._evaluate_low_event(
                                    symbol,
                                    event,
                                    mode=StrategyEvaluationMode.REPLAY,
                                    delivery_required=False,
                                    suppression_reason="historical_replay",
                                    position_provider=historical_position_provider,
                                )
                                replayed += 1
                            self.health.replayed_events += replayed
                    checkpoint_after = await asyncio.to_thread(self._checkpoint, symbol)
                    if checkpoint_after is None:
                        raise RuntimeError("strategy bootstrap did not create a checkpoint")
                    state.low = state.low[-self.settings.low_window :]
                    state.high = state.high[-self.settings.high_window :]
                    outputs.append(
                        SymbolBootstrap(
                            symbol,
                            len(state.low),
                            len(state.high),
                            checkpoint_before,
                            checkpoint_after.last_event_time,
                            len(missed),
                            replayed,
                            reset,
                            initial_evaluation,
                        )
                    )
                self.health.bootstrapped = True
                self.health.bootstrap_runs += 1
                self.health.last_error = None
                return StrategyBootstrapResult(tuple(outputs), recovered)
            except Exception as exc:
                self._record_failure(exc)
                raise

    async def _drain_pending(self, symbol: str) -> None:
        state = self._states[symbol]
        for event_time in sorted(tuple(state.pending_low)):
            event = state.pending_low[event_time]
            if not self._barrier_ready(symbol, event):
                break
            await self._evaluate_low_event(symbol, event)
            state.pending_low.pop(event_time, None)

    async def handle_market_event(self, event: MarketEvent) -> None:
        async with self._processing_lock:
            self.health.received_events += 1
            self.health.last_market_event_time = event.event_time
            try:
                if not self.health.bootstrapped:
                    raise RuntimeError("strategy engine must be bootstrapped before live events")
                if self.settings.require_market_sequence and event.sequence <= 0:
                    raise ValueError("live strategy input must carry a market-bus sequence")
                if event.kind is not MarketEventKind.CANDLE_CLOSED:
                    self.health.ignored_events += 1
                    return
                if event.price_type != "contract":
                    self.health.ignored_events += 1
                    return
                config = self.configs.get(event.symbol)
                if config is None:
                    raise KeyError(f"unconfigured strategy symbol: {event.symbol}")
                if event.timeframe not in {config.low_timeframe, config.high_timeframe}:
                    self.health.ignored_events += 1
                    return

                candle = candle_from_market_event(event)
                state = self._states[event.symbol]
                if event.timeframe == config.high_timeframe:
                    changed = self._append_history(
                        state.high,
                        candle,
                        self.settings.high_window,
                    )
                    if not changed:
                        self.health.duplicate_events += 1
                        return
                    self.health.high_events += 1
                    await self._drain_pending(event.symbol)
                else:
                    changed = self._append_history(
                        state.low,
                        candle,
                        self.settings.low_window,
                    )
                    if not changed:
                        self.health.duplicate_events += 1
                        return
                    self.health.low_events += 1
                    existing = state.pending_low.get(event.event_time)
                    if existing is not None and existing.event_id != event.event_id:
                        raise RuntimeError(
                            f"pending low-timeframe event changed at {event.event_time}"
                        )
                    state.pending_low[event.event_time] = event
                    if len(state.pending_low) > self.settings.max_pending_low_events:
                        raise StrategyBarrierError(
                            f"high-timeframe barrier backlog exceeded for {event.symbol}"
                        )
                    if not self._barrier_ready(event.symbol, event):
                        self.health.barrier_waits += 1
                    await self._drain_pending(event.symbol)
                self.health.last_error = None
            except Exception as exc:
                self._record_failure(exc)
                raise

    async def close(self) -> None:
        await self.intent_bus.close()

    async def health_snapshot(self) -> dict[str, Any]:
        pending = {symbol: len(state.pending_low) for symbol, state in self._states.items()}
        return {
            "engine": self.health.snapshot(pending),
            "store": await asyncio.to_thread(self.store.metrics),
            "intent_bus": await self.intent_bus.health_snapshot(),
        }
