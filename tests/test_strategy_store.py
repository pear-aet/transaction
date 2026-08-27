from dataclasses import replace

import pytest

from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent
from crypto_quant.strategy_store import (
    StrategyCheckpointError,
    StrategyDeliveryState,
    StrategyEvaluationMode,
    StrategyIntentConflict,
    StrategyStore,
)


def intent(intent_id: str = "intent-1", decision_time: int = 100) -> StrategyIntent:
    return StrategyIntent(
        intent_id=intent_id,
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="primary",
        symbol="BTC/USDT:USDT",
        action=StrategyAction.ENTER_LONG,
        decision_time=decision_time,
        created_at=decision_time + 1,
        reference_price=100,
        stop_price=99,
        reason="test",
        source_event_id=f"market-{decision_time}",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="hash",
        evidence={"trend": "up"},
    )


def commit(
    store: StrategyStore,
    value: StrategyIntent | None,
    *,
    event_time: int,
    event_id: str,
    updated_at: int,
    parameter_hash: str = "hash",
    mode: StrategyEvaluationMode = StrategyEvaluationMode.LIVE,
    delivery_required: bool = True,
    suppression_reason: str | None = None,
):
    return store.commit_evaluation(
        instance_id="primary",
        symbol="BTC/USDT:USDT",
        event_time=event_time,
        event_id=event_id,
        updated_at=updated_at,
        strategy_id="test",
        strategy_version="1.0.0",
        parameter_hash=parameter_hash,
        high_timeframe="1h",
        low_timeframe="15m",
        intent=value,
        mode=mode,
        delivery_required=delivery_required,
        suppression_reason=suppression_reason,
    )


def test_evaluation_checkpoint_and_intent_are_atomic_and_idempotent(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    value = intent()
    first = commit(
        store,
        value,
        event_time=value.decision_time,
        event_id=value.source_event_id,
        updated_at=101,
    )
    duplicate = commit(
        store,
        replace(value, created_at=999),
        event_time=value.decision_time,
        event_id=value.source_event_id,
        updated_at=102,
    )

    assert first.intent_inserted is True
    assert first.stored_intent.intent.storage_sequence == 1
    assert duplicate.event_duplicate is True
    assert store.load_checkpoint("primary", value.symbol).last_event_time == 100
    pending = store.pending_intents(instance_id="primary")
    assert [item.intent_id for item in pending] == [value.intent_id]
    assert pending[0].storage_sequence == 1
    assert store.mark_published(value.intent_id, 200) is True
    assert store.mark_published(value.intent_id, 201) is False
    stored = store.list_intents(instance_id="primary")
    assert stored[0].intent.intent_id == value.intent_id
    assert stored[0].delivery_state is StrategyDeliveryState.PUBLISHED
    assert stored[0].published_at == 200
    assert store.metrics() == {
        "checkpoints": 1,
        "evaluations": 1,
        "intents": 1,
        "pending_intents": 0,
        "published_intents": 1,
        "suppressed_intents": 0,
        "latest_intent_sequence": 1,
    }


def test_checkpoint_cannot_move_backwards_or_change_same_event_time(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    value = intent()
    commit(store, value, event_time=100, event_id="market-100", updated_at=101)
    with pytest.raises(StrategyCheckpointError, match="backwards"):
        commit(
            store,
            None,
            event_time=99,
            event_id="market-99",
            updated_at=102,
        )
    with pytest.raises(StrategyCheckpointError, match="changed"):
        commit(
            store,
            None,
            event_time=100,
            event_id="different-market-event",
            updated_at=102,
        )


def test_same_market_event_cannot_change_its_strategy_decision(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    value = intent()
    commit(store, value, event_time=100, event_id="market-100", updated_at=101)
    with pytest.raises(StrategyIntentConflict, match="decision changed"):
        commit(
            store,
            None,
            event_time=100,
            event_id="market-100",
            updated_at=102,
        )


def test_strategy_instance_is_pinned_to_version_and_parameter_hash(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    value = intent()
    commit(store, value, event_time=100, event_id="market-100", updated_at=101)
    with pytest.raises(StrategyCheckpointError, match="configuration changed"):
        commit(
            store,
            None,
            event_time=200,
            event_id="market-200",
            updated_at=201,
            parameter_hash="different-hash",
        )


def test_replay_intent_is_persisted_but_never_enters_live_outbox(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    value = intent()
    result = commit(
        store,
        value,
        event_time=100,
        event_id="market-100",
        updated_at=101,
        mode=StrategyEvaluationMode.REPLAY,
        delivery_required=False,
        suppression_reason="historical_replay",
    )

    assert result.stored_intent.delivery_state is StrategyDeliveryState.SUPPRESSED
    assert result.stored_intent.suppression_reason == "historical_replay"
    assert store.pending_intents(instance_id="primary") == []
    replay_log = store.load_intents(after_sequence=0)
    assert len(replay_log) == 1
    assert replay_log[0].intent.storage_sequence == 1
    assert replay_log[0].delivery_state is StrategyDeliveryState.SUPPRESSED
    assert store.latest_intent_sequence() == 1
    with pytest.raises(ValueError, match="suppressed"):
        store.mark_published(value.intent_id, 200)


def test_intent_id_collision_is_rejected_without_advancing_checkpoint(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    first = intent()
    commit(
        store,
        first,
        event_time=first.decision_time,
        event_id=first.source_event_id,
        updated_at=101,
    )
    collision = replace(
        intent(intent_id=first.intent_id, decision_time=200),
        source_event_id="market-200",
        reason="different",
    )
    with pytest.raises(StrategyIntentConflict, match="collision"):
        commit(
            store,
            collision,
            event_time=collision.decision_time,
            event_id=collision.source_event_id,
            updated_at=201,
        )
    assert store.load_checkpoint("primary", first.symbol).last_event_time == 100
    assert len(store.list_intents(instance_id="primary")) == 1
