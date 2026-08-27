import json

import pytest

from crypto_quant.market_stream import (
    BinanceFuturesStream,
    ClosedCandleEvent,
    MarkPriceEvent,
    binance_stream_symbol,
    parse_binance_stream_message,
)


def test_symbol_normalization_for_binance_stream() -> None:
    assert binance_stream_symbol("BTC/USDT:USDT") == "btcusdt"


def test_open_kline_is_not_emitted() -> None:
    payload = {
        "e": "kline",
        "E": 60_000,
        "s": "BTCUSDT",
        "k": {
            "t": 0,
            "T": 59_999,
            "s": "BTCUSDT",
            "i": "1m",
            "o": "10",
            "h": "12",
            "l": "9",
            "c": "11",
            "v": "4",
            "x": False,
        },
    }
    assert parse_binance_stream_message(json.dumps(payload)) is None


def test_closed_kline_becomes_typed_event() -> None:
    payload = {
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline",
            "E": 60_001,
            "s": "BTCUSDT",
            "k": {
                "t": 0,
                "T": 59_999,
                "s": "BTCUSDT",
                "i": "1m",
                "o": "10",
                "h": "12",
                "l": "9",
                "c": "11",
                "v": "4",
                "x": True,
            },
        },
    }
    event = parse_binance_stream_message(payload)
    assert isinstance(event, ClosedCandleEvent)
    assert event.symbol == "BTCUSDT"
    assert event.timeframe == "1m"
    assert event.candle.timestamp == 0
    assert event.candle.close == 11


def test_invalid_stream_interval_is_rejected() -> None:
    payload = {
        "e": "kline",
        "E": 1,
        "s": "BTCUSDT",
        "k": {
            "t": 0,
            "T": 30_000,
            "i": "1m",
            "o": "10",
            "h": "12",
            "l": "9",
            "c": "11",
            "v": "4",
            "x": True,
        },
    }
    with pytest.raises(ValueError, match="interval"):
        parse_binance_stream_message(payload)


def test_mark_price_message_is_typed() -> None:
    event = parse_binance_stream_message(
        {
            "e": "markPriceUpdate",
            "E": 100,
            "s": "BTCUSDT",
            "p": "62000.5",
            "i": "61990.0",
            "r": "0.0001",
            "T": 200,
        }
    )
    assert isinstance(event, MarkPriceEvent)
    assert event.mark_price == 62000.5
    assert event.index_price == 61990.0
    assert event.funding_rate == 0.0001


def test_combined_stream_names_cover_all_symbols_and_timeframes() -> None:
    stream = BinanceFuturesStream(
        ["BTC/USDT:USDT", "ETH/USDT:USDT"], ["15m", "1h"], include_mark_price=True
    )
    names = stream.stream_names()
    assert "btcusdt@kline_15m" in names
    assert "btcusdt@kline_1h" in names
    assert "ethusdt@kline_15m" in names
    assert "ethusdt@markPrice@1s" in names
    assert len(names) == 6
