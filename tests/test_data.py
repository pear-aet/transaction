import pytest

from crypto_quant.data import CcxtDataSource, validate_candles
from crypto_quant.domain import Candle


class FakeExchange:
    has = {"fetchFundingRateHistory": True}

    def __init__(self) -> None:
        self.closed = False

    @staticmethod
    def parse_timeframe(value: str) -> int:
        assert value == "1m"
        return 60

    @staticmethod
    def milliseconds() -> int:
        return 300_000

    @staticmethod
    def fetch_ohlcv(symbol, timeframe, since, limit):
        rows = [
            [0, 10, 11, 9, 10, 1],
            [60_000, 10, 12, 9, 11, 1],
            [120_000, 11, 12, 10, 11, 1],
            [300_000, 11, 12, 10, 11, 1],
        ]
        selected = [row for row in rows if since is None or row[0] >= since]
        return selected[:limit]

    @staticmethod
    def fetch_funding_rate_history(symbol, since, limit):
        rows = [
            {"timestamp": 10, "fundingRate": 0.0001},
            {"timestamp": 20, "fundingRate": -0.0002},
            {"timestamp": 30, "fundingRate": 0.0003},
        ]
        selected = [row for row in rows if since is None or row["timestamp"] >= since]
        return selected[:limit]

    def close(self) -> None:
        self.closed = True


def test_candle_pagination_deduplicates_and_drops_open_bar(monkeypatch) -> None:
    source = CcxtDataSource()
    fake = FakeExchange()
    monkeypatch.setattr(CcxtDataSource, "_exchange", lambda self: fake)
    result = source.fetch_candles("BTC/USDT:USDT", "1m", 0, limit=2, max_candles=10)
    assert [candle.timestamp for candle in result] == [0, 60_000, 120_000]
    assert fake.closed


def test_funding_pagination(monkeypatch) -> None:
    source = CcxtDataSource()
    monkeypatch.setattr(CcxtDataSource, "_exchange", lambda self: FakeExchange())
    result = source.fetch_funding_rates("BTC/USDT:USDT", 0, limit=2, max_rates=10)
    assert [(rate.timestamp, rate.rate) for rate in result] == [
        (10, 0.0001),
        (20, -0.0002),
        (30, 0.0003),
    ]


def test_gap_validation_rejects_incomplete_history() -> None:
    candles = [Candle(0, 10, 11, 9, 10), Candle(120_000, 10, 11, 9, 10)]
    with pytest.raises(ValueError, match="gap"):
        validate_candles(candles, 60_000)


class RulesExchange(FakeExchange):
    has = {"fetchLeverageTiers": True}

    @staticmethod
    def load_markets():
        return {}

    @staticmethod
    def market(symbol):
        return {
            "precision": {"price": 2, "amount": 3},
            "limits": {},
            "info": {
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.002"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ]
            },
        }

    @staticmethod
    def fetch_leverage_tiers(symbols):
        return {
            symbols[0]: [
                {
                    "minNotional": 0,
                    "maxNotional": 50_000,
                    "maintenanceMarginRate": 0.004,
                    "maxLeverage": 100,
                    "info": {"cum": "0"},
                }
            ]
        }


def test_market_rules_parse_filters_and_risk_brackets(monkeypatch) -> None:
    monkeypatch.setattr(CcxtDataSource, "_exchange", lambda self: RulesExchange())
    rules = CcxtDataSource().fetch_market_rules("BTC/USDT:USDT")
    assert rules.tick_size == 0.1
    assert rules.step_size == 0.001
    assert rules.min_quantity == 0.002
    assert rules.min_notional == 5
    assert rules.max_leverage == 100
    assert rules.brackets[0].maintenance_margin_rate == 0.004
