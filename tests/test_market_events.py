import pytest

from crypto_quant.domain import Candle
from crypto_quant.market_events import (
    CandleClosedPayload,
    MarketEventFilter,
    MarketEventKind,
    MarkPricePayload,
    closed_candle_market_event,
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


def test_closed_candle_event_is_deterministic_and_revision_sensitive() -> None:
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
    assert first.source_time == 1_000
    assert first.latency_ms == 10
    assert first.sequence == 0
    sequenced = first.with_sequence(7).to_dict()
    assert sequenced["sequence"] == 7
    assert sequenced["schema_version"] == 1


def test_mark_price_event_and_filters_are_strict() -> None:
    event = mark_price_market_event(
        MarkPriceEvent("BTCUSDT", 101.0, 100.9, 0.0001, 3_600_000, 2_000),
        "BTC/USDT:USDT",
        received_at=2_010,
    )
    matching = MarketEventFilter(
        kinds=frozenset({MarketEventKind.MARK_PRICE}),
        symbols=frozenset({"BTC/USDT:USDT"}),
    )
    wrong_timeframe = MarketEventFilter(timeframes=frozenset({"15m"}))
    wrong_price_type = MarketEventFilter(price_types=frozenset({"mark"}))

    assert matching.matches(event)
    assert not wrong_timeframe.matches(event)
    assert not wrong_price_type.matches(event)
    assert event.to_dict()["payload"]["funding_rate"] == 0.0001


def test_candle_price_type_filter_matches_payload() -> None:
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


def test_invalid_payloads_are_rejected() -> None:
    with pytest.raises(ValueError, match="volume"):
        CandleClosedPayload(0, 1.0, 1.0, 1.0, 1.0, -1.0)
    with pytest.raises(ValueError, match="mark_price"):
        MarkPricePayload(float("nan"), 1.0, None, None)
