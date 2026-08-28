from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent
from crypto_quant.strategy_store import StrategyEvaluationMode, StrategyStore


def intent(
    index: int,
    *,
    instance_id: str,
    symbol: str,
) -> StrategyIntent:
    decision_time = index * 100
    return StrategyIntent(
        intent_id=f"intent-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id=instance_id,
        symbol=symbol,
        action=StrategyAction.ENTER_LONG,
        decision_time=decision_time,
        created_at=decision_time + 1,
        reference_price=100 + index,
        stop_price=99 + index,
        reason="query_filter_test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="hash",
        evidence={},
    )


def commit(store: StrategyStore, value: StrategyIntent) -> None:
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
        mode=StrategyEvaluationMode.LIVE,
        delivery_required=True,
    )


def test_static_query_filters_cover_all_optional_dimensions(tmp_path) -> None:
    store = StrategyStore(tmp_path / "quant.db")
    btc_primary = intent(1, instance_id="primary", symbol="BTC/USDT:USDT")
    eth_primary = intent(2, instance_id="primary", symbol="ETH/USDT:USDT")
    btc_secondary = intent(3, instance_id="secondary", symbol="BTC/USDT:USDT")
    for value in (btc_primary, eth_primary, btc_secondary):
        commit(store, value)

    assert [item.intent_id for item in store.pending_intents(instance_id="primary")] == [
        btc_primary.intent_id,
        eth_primary.intent_id,
    ]
    assert [
        item.intent.intent_id
        for item in store.load_intents(
            after_sequence=0,
            through_sequence=2,
            instance_id="primary",
            symbol="ETH/USDT:USDT",
        )
    ] == [eth_primary.intent_id]
    assert store.load_intents(after_sequence=3, through_sequence=2) == []
    assert [
        item.intent.intent_id
        for item in store.list_intents(
            instance_id="secondary",
            symbol="BTC/USDT:USDT",
        )
    ] == [btc_secondary.intent_id]
