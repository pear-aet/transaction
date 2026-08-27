from dataclasses import replace

import pytest

from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent
from crypto_quant.strategy_store import (
    StrategyCheckpointError,
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


def test_evaluation_checkpoint_and_intent_are_atomic_and_idempotent(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    value = intent()
    first = store.commit_evaluation(
        instance_id="primary",
        symbol=value.symbol,
        event_time=value.decision_time,
        event_id=value.source_event_id,
        updated_at=101,
        intent=value,
    )
    duplicate = store.commit_evaluation(
        instance_id="primary",
        symbol=value.symbol,
        event_time=value.decision_time,
        event_id=value.source_event_id,
        updated_at=102,
        intent=replace(value, created_at=999),
    )

    assert first.intent_inserted is True
    assert duplicate.event_duplicate is True
    assert store.load_checkpoint("primary", value.symbol).last_event_time == 100
    assert store.pending_intents(instance_id="primary") == [value]
    assert store.mark_published(value.intent_id, 200) is True
    assert store.mark_published(value.intent_id, 201) is False
    stored = store.list_intents(instance_id="primary")
    assert stored[0].intent == value
    assert stored[0].published_at == 200
    assert store.metrics() == {"checkpoints": 1, "intents": 1, "pending_intents": 0}


def test_checkpoint_cannot_move_backwards_or_change_same_event_time(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    value = intent()
    store.commit_evaluation(
        instance_id="primary",
        symbol=value.symbol,
        event_time=100,
        event_id="market-100",
        updated_at=101,
        intent=value,
    )
    with pytest.raises(StrategyCheckpointError, match="backwards"):
        store.commit_evaluation(
            instance_id="primary",
            symbol=value.symbol,
            event_time=99,
            event_id="market-99",
            updated_at=102,
            intent=None,
        )
    with pytest.raises(StrategyCheckpointError, match="changed"):
        store.commit_evaluation(
            instance_id="primary",
            symbol=value.symbol,
            event_time=100,
            event_id="different-market-event",
            updated_at=102,
            intent=None,
        )


def test_intent_id_collision_is_rejected_without_advancing_checkpoint(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    first = intent()
    store.commit_evaluation(
        instance_id="primary",
        symbol=first.symbol,
        event_time=first.decision_time,
        event_id=first.source_event_id,
        updated_at=101,
        intent=first,
    )
    collision = replace(
        intent(intent_id=first.intent_id, decision_time=200),
        source_event_id="market-200",
        reason="different",
    )
    with pytest.raises(StrategyIntentConflict, match="collision"):
        store.commit_evaluation(
            instance_id="primary",
            symbol=collision.symbol,
            event_time=collision.decision_time,
            event_id=collision.source_event_id,
            updated_at=201,
            intent=collision,
        )
    assert store.load_checkpoint("primary", first.symbol).last_event_time == 100
    assert len(store.list_intents(instance_id="primary")) == 1
