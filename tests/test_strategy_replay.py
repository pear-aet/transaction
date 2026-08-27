import pytest

from crypto_quant.strategy_contracts import (
    StrategyAction,
    StrategyIntent,
    StrategyIntentFilter,
)
from crypto_quant.strategy_replay import (
    DurableIntentFilter,
    StrategyConsumerFilterMismatch,
    StrategyReplayLedger,
)
from crypto_quant.strategy_store import (
    StrategyDeliveryState,
    StrategyEvaluationMode,
    StrategyStore,
)


def intent(index: int, symbol: str = "BTC/USDT:USDT") -> StrategyIntent:
    return StrategyIntent(
        intent_id=f"intent-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="primary",
        symbol=symbol,
        action=StrategyAction.ENTER_LONG,
        decision_time=index * 100,
        created_at=index * 100 + 1,
        reference_price=100 + index,
        stop_price=99 + index,
        reason="test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="hash",
        evidence={},
    )


def commit(
    store: StrategyStore,
    value: StrategyIntent,
    *,
    mode: StrategyEvaluationMode,
    delivery_required: bool,
) -> None:
    store.commit_evaluation(
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


def seed(store: StrategyStore) -> None:
    live = intent(1)
    replay = intent(2)
    commit(
        store,
        live,
        mode=StrategyEvaluationMode.LIVE,
        delivery_required=True,
    )
    store.mark_published(live.intent_id, 200)
    commit(
        store,
        replay,
        mode=StrategyEvaluationMode.REPLAY,
        delivery_required=False,
    )


def test_default_durable_filter_never_replays_suppressed_history(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    seed(store)
    ledger = StrategyReplayLedger(store)

    batch = ledger.read_batch("risk-v1")
    assert batch.start_sequence == 0
    assert batch.high_watermark == 2
    assert batch.scanned_through == 2
    assert batch.skipped == 1
    assert [item.intent.intent_id for item in batch.intents] == ["intent-1"]
    assert batch.intents[0].delivery_state is StrategyDeliveryState.PUBLISHED

    offset = ledger.commit_batch(batch, updated_at=300)
    assert offset.sequence == 2
    empty = ledger.read_batch("risk-v1")
    assert empty.intents == ()
    assert empty.scanned_through == 2


def test_audit_consumer_must_explicitly_request_suppressed_intents(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    seed(store)
    ledger = StrategyReplayLedger(store)
    audit_filter = DurableIntentFilter(
        delivery_states=frozenset(
            {StrategyDeliveryState.PUBLISHED, StrategyDeliveryState.SUPPRESSED}
        )
    )

    batch = ledger.read_batch("audit-v1", audit_filter)
    assert [item.intent.intent_id for item in batch.intents] == ["intent-1", "intent-2"]
    assert [item.delivery_state for item in batch.intents] == [
        StrategyDeliveryState.PUBLISHED,
        StrategyDeliveryState.SUPPRESSED,
    ]


def test_consumer_filter_is_pinned_and_stale_batches_cannot_commit(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    seed(store)
    ledger = StrategyReplayLedger(store)
    first = ledger.read_batch("risk-v1", limit=1)
    stale = ledger.read_batch("risk-v1", limit=2)
    ledger.commit_batch(first, updated_at=300)

    with pytest.raises(ValueError, match="stale replay batch"):
        ledger.commit_batch(stale, updated_at=301)
    with pytest.raises(StrategyConsumerFilterMismatch, match="filter changed"):
        ledger.read_batch(
            "risk-v1",
            DurableIntentFilter(
                intent_filter=StrategyIntentFilter(
                    symbols=frozenset({"ETH/USDT:USDT"})
                )
            ),
        )
