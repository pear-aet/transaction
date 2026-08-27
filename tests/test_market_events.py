from dataclasses import replace

import pytest

from crypto_quant.domain import Candle, FundingRate
from crypto_quant.market_events import (
    CandleClosedPayload,
    MarketEvent,
    MarketEventFilter,
    MarketEventKind,
    MarkPricePayload,
    candle_from_market_event,
    candle_market_event,
    closed_candle_market_event,
    funding_from_market_event,
    funding_rate_market_event,
    mark_price_market_event,
)
from crypto_quant.market_stream import ClosedCandleEvent, MarkPriceEvent


def closed_event(close: float = 101.0) -> ClosedCandleEvent:
    return ClosedCandleEvent(
        "BTCUSDT",
        "15m",
        Candle(1_000, 100.0, 102.0, 99.0, close, 12.5),
        901_000,
    )


def test_closed_candle_event_is_deterministic_revision_sensitive_and_round_trips() -> None:
    first = closed_candle_market_event(
        closed_event(),
        "BTC/USDT:USDT",
        received_at=901_010,
    )
    duplicate = closed_candle_market_event(
        closed_event(),
        "BTC/USDT:USDT",
        received_at=901_020,
    )
    revision = closed_candle_market_event(
        closed_event(101.5),
        "BTC/USDT:USDT",
        received_at=901_030,
    )

    assert first.event_id == duplicate.event_id
    assert first.event_id != revision.event_id
    assert first.kind is MarketEventKind.CANDLE_CLOSED
    assert first.timeframe == "15m"
    assert first.price_type == "contract"
    assert first.event_time == 901_000
    assert first.source_time == 901_000
    assert first.latency_ms == 10
    assert first.partition_key == "BTC/USDT:USDT:candle:contract:15m"
    assert first.sequence == 0
    assert candle_from_market_event(first) == closed_event().candle
    sequenced = first.with_sequence(7).with_storage_sequence(3)
    assert MarketEvent.from_dict(sequenced.to_dict()) == sequenced


def test_same_close_orders_high_timeframe_before_low_timeframe() -> None:
    hourly = candle_market_event(
        "BTC/USDT:USDT",
        "1h",
        Candle(0, 100, 110, 90, 105, 1),
        received_at=3_600_001,
    )
    fifteen = candle_market_event(
        "BTC/USDT:USDT",
        "15m",
        Candle(2_700_000, 105, 108, 100, 102, 1),
        received_at=3_600_001,
    )
    assert hourly.event_time == fifteen.event_time == 3_600_000
    assert sorted([fifteen, hourly], key=lambda item: item.sort_key) == [hourly, fifteen]


def test_mark_price_event_and_filters_are_strict() -> None:
    event = mark_price_market_event(
        MarkPriceEvent("BTCUSDT", 101.0, 100.9, 0.0001, 3_600_000, 2_000),
        "BTC/USDT:USDT",
        received_at=2_010,
    )
    matching = MarketEventFilter(
        kinds=frozenset({MarketEventKind.MARK_PRICE}),
        symbols=frozenset({"BTC/USDT:USDT"}),
        price_types=frozenset({"mark"}),
    )
    wrong_timeframe = MarketEventFilter(timeframes=frozenset({"15m"}))
    wrong_price_type = MarketEventFilter(price_types=frozenset({"contract"}))

    assert matching.matches(event)
    assert not wrong_timeframe.matches(event)
    assert not wrong_price_type.matches(event)
    assert event.partition_key == "BTC/USDT:USDT:mark_price"
    assert event.to_dict()["payload"]["funding_rate"] == 0.0001


def test_funding_event_round_trips_as_a_durable_market_fact() -> None:
    funding = FundingRate(28_800_000, -0.0002)
    event = funding_rate_market_event(
        "BTC/USDT:USDT",
        funding,
        received_at=28_800_001,
    )
    assert event.kind is MarketEventKind.FUNDING_RATE
    assert event.durable is True
    assert event.partition_key == "BTC/USDT:USDT:funding_rate"
    assert funding_from_market_event(event) == funding
    assert MarketEvent.from_dict(event.to_dict()) == event


def test_candle_price_type_filter_matches_envelope() -> None:
    event = closed_candle_market_event(
        closed_event(),
        "BTC/USDT:USDT",
        price_type="contract",
        received_at=901_010,
    )
    contract_filter = MarketEventFilter(price_types=frozenset({"contract"}))
    mark_filter = MarketEventFilter(price_types=frozenset({"mark"}))
    assert contract_filter.matches(event)
    assert not mark_filter.matches(event)


def test_invalid_payloads_and_sequences_are_rejected() -> None:
    with pytest.raises(ValueError, match="volume"):
        CandleClosedPayload(0, 899_999, 1.0, 1.0, 1.0, 1.0, -1.0)
    with pytest.raises(ValueError, match="mark_price"):
        MarkPricePayload(float("nan"), 1.0, None, None)
    event = funding_rate_market_event("BTC", FundingRate(1, 0.0001), received_at=2)
    with pytest.raises(ValueError, match="storage_sequence"):
        replace(event, storage_sequence=0)
