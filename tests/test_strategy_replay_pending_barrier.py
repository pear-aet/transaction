import pytest

from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent
from crypto_quant.strategy_replay import DurableIntentFilter, StrategyReplayLedger
from crypto_quant.strategy_store import (
    StrategyDeliveryState,
    StrategyEvaluationMode,
    StrategyStore,
)

SYMBOL = "BTC/USDT:USDT"


def intent(index: int, decision_time: int) -> StrategyIntent:
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
        reason="pending_barrier_test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
    )


def commit(
    store: StrategyStore,
    value: StrategyIntent,
    *,
    mode: StrategyEvaluationMode,
    delivery_required: bool,
) -> StrategyIntent:
    result = store.commit_evaluation(
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
        mode=mode,
        delivery_required=delivery_required,
        suppression_reason=None if delivery_required else "historical_replay",
    )
    assert result.stored_intent is not None
    return result.stored_intent.intent


def test_pending_intent_blocks_cursor_from_skipping_later_terminal_rows(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    pending = commit(
        store,
        intent(1, 100),
        mode=StrategyEvaluationMode.LIVE,
        delivery_required=True,
    )
    suppressed = commit(
        store,
        intent(2, 200),
        mode=StrategyEvaluationMode.REPLAY,
        delivery_required=False,
    )
    ledger = StrategyReplayLedger(store)

    blocked = ledger.read_batch("risk-v1")
    assert pending.storage_sequence == 1
    assert suppressed.storage_sequence == 2
    assert blocked.high_watermark == 2
    assert blocked.scanned_through == 0
    assert blocked.intents == ()
    assert blocked.skipped == 0

    store.mark_published(pending.intent_id, 300)
    released = ledger.read_batch("risk-v1")
    assert released.scanned_through == 2
    assert [item.intent.intent_id for item in released.intents] == [pending.intent_id]
    assert released.skipped == 1

    offset = ledger.commit_batch(released, updated_at=301)
    assert offset.sequence == 2


def test_pending_delivery_state_cannot_be_selected_for_durable_replay() -> None:
    with pytest.raises(ValueError, match="mutable pending"):
        DurableIntentFilter(delivery_states=frozenset({StrategyDeliveryState.PENDING}))
