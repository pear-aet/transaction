from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any

from .domain import Candle, FundingRate, MarketRules, RiskBracket


@dataclass(slots=True)
class CcxtDataSource:
    exchange_id: str = "binanceusdm"
    market_type: str = "swap"
    max_retries: int = 4

    def _exchange(self) -> Any:
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover - dependency error path
            raise RuntimeError("Install project dependencies before fetching market data") from exc
        exchange_class = getattr(ccxt, self.exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"unsupported CCXT exchange: {self.exchange_id}")
        settings: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {"defaultType": self.market_type, "adjustForTimeDifference": True},
        }
        api_key = os.getenv("CQ_BINANCE_READ_ONLY_API_KEY", "")
        secret = os.getenv("CQ_BINANCE_READ_ONLY_SECRET", "")
        if api_key and secret:
            settings.update({"apiKey": api_key, "secret": secret})
        return exchange_class(settings)

    def _retry(self, function, *args, **kwargs):
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return function(*args, **kwargs)
            except Exception as exc:  # CCXT normalizes several transient exception classes.
                last_error = exc
                if attempt + 1 == self.max_retries:
                    break
                time.sleep(min(8.0, 0.35 * 2**attempt + random.random() * 0.2))
        raise RuntimeError(
            f"market data request failed after {self.max_retries} attempts"
        ) from last_error

    def _fetch_candle_rows(
        self,
        exchange: Any,
        fetcher,
        symbol: str,
        timeframe: str,
        since: int | None,
        limit: int,
        max_candles: int,
    ) -> list[Candle]:
        timeframe_value = int(exchange.parse_timeframe(timeframe) * 1000)
        cursor = since
        rows: list[list[float]] = []
        while len(rows) < max_candles:
            batch_limit = min(limit, max_candles - len(rows))
            batch = self._retry(fetcher, symbol, timeframe, cursor, batch_limit)
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][0]) + timeframe_value
            if cursor is not None and next_cursor <= cursor:
                raise ValueError("non-advancing market data cursor")
            cursor = next_cursor
            if len(batch) < batch_limit or cursor >= exchange.milliseconds():
                break
        now = exchange.milliseconds()
        deduped = {int(row[0]): row for row in rows if int(row[0]) + timeframe_value <= now}
        candles = [
            Candle(
                int(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]) if len(row) > 5 else 0.0,
            )
            for row in sorted(deduped.values(), key=lambda item: item[0])
        ]
        validate_candles(candles, timeframe_value)
        return candles

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int = 1000,
        max_candles: int = 5000,
    ) -> list[Candle]:
        exchange = self._exchange()
        try:
            return self._fetch_candle_rows(
                exchange, exchange.fetch_ohlcv, symbol, timeframe, since, limit, max_candles
            )
        finally:
            close = getattr(exchange, "close", None)
            if callable(close):
                close()

    def fetch_mark_candles(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int = 1000,
        max_candles: int = 5000,
    ) -> list[Candle]:
        exchange = self._exchange()
        try:
            if exchange.has.get("fetchMarkOHLCV") and hasattr(exchange, "fetch_mark_ohlcv"):
                fetcher = exchange.fetch_mark_ohlcv
            else:

                def fetcher(s, t, cursor, size):
                    return exchange.fetch_ohlcv(s, t, cursor, size, {"price": "mark"})

            return self._fetch_candle_rows(
                exchange, fetcher, symbol, timeframe, since, limit, max_candles
            )
        finally:
            close = getattr(exchange, "close", None)
            if callable(close):
                close()

    def fetch_funding_rates(
        self,
        symbol: str,
        since: int | None = None,
        limit: int = 1000,
        max_rates: int = 5000,
    ) -> list[FundingRate]:
        exchange = self._exchange()
        try:
            if not exchange.has.get("fetchFundingRateHistory"):
                return []
            cursor = since
            rows: list[dict[str, Any]] = []
            while len(rows) < max_rates:
                batch_limit = min(limit, max_rates - len(rows))
                batch = self._retry(
                    exchange.fetch_funding_rate_history, symbol, cursor, batch_limit
                )
                if not batch:
                    break
                rows.extend(batch)
                next_cursor = int(batch[-1]["timestamp"]) + 1
                if cursor is not None and next_cursor <= cursor:
                    break
                cursor = next_cursor
                if len(batch) < batch_limit:
                    break
            unique = {
                int(item["timestamp"]): item
                for item in rows
                if item.get("timestamp") is not None and item.get("fundingRate") is not None
            }
            return [
                FundingRate(timestamp, float(unique[timestamp]["fundingRate"]))
                for timestamp in sorted(unique)
            ]
        finally:
            close = getattr(exchange, "close", None)
            if callable(close):
                close()

    def fetch_market_rules(self, symbol: str) -> MarketRules:
        """Fetch public filters and authenticated leverage tiers when available."""
        exchange = self._exchange()
        try:
            self._retry(exchange.load_markets)
            market = exchange.market(symbol)
            filters = {
                item.get("filterType"): item for item in market.get("info", {}).get("filters", [])
            }
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            notional_filter = filters.get("MIN_NOTIONAL", {})
            tick = float(price_filter.get("tickSize") or 10 ** -int(market["precision"]["price"]))
            step = float(lot_filter.get("stepSize") or 10 ** -int(market["precision"]["amount"]))
            minimum_quantity = float(
                lot_filter.get("minQty")
                or market.get("limits", {}).get("amount", {}).get("min")
                or 0
            )
            minimum_notional = float(
                notional_filter.get("notional")
                or notional_filter.get("minNotional")
                or market.get("limits", {}).get("cost", {}).get("min")
                or 0
            )
            brackets: list[RiskBracket] = []
            maximum_leverage = 1.0
            if exchange.has.get("fetchLeverageTiers") and hasattr(exchange, "fetch_leverage_tiers"):
                try:
                    tiers = self._retry(exchange.fetch_leverage_tiers, [symbol]).get(symbol, [])
                    for tier in tiers:
                        brackets.append(
                            RiskBracket(
                                float(tier["minNotional"]),
                                float(tier["maxNotional"]),
                                float(tier["maintenanceMarginRate"]),
                                float(tier.get("info", {}).get("cum", 0.0)),
                            )
                        )
                        maximum_leverage = max(maximum_leverage, float(tier["maxLeverage"]))
                except Exception:
                    # The Binance bracket endpoint is signed; public research still works
                    # with a conservative tier until read-only credentials are configured.
                    brackets = []
            if not brackets:
                brackets = [RiskBracket(0.0, 1e30, 0.01, 0.0)]
                maximum_leverage = 20.0
            return MarketRules(
                symbol,
                tick,
                step,
                minimum_quantity,
                minimum_notional,
                maximum_leverage,
                tuple(brackets),
            )
        finally:
            close = getattr(exchange, "close", None)
            if callable(close):
                close()


def timeframe_ms(value: str) -> int:
    unit = value[-1]
    amount = int(value[:-1])
    factors = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    if unit not in factors or amount <= 0:
        raise ValueError(f"unsupported timeframe: {value}")
    return amount * factors[unit]


def days_ago_ms(days: int) -> int:
    if days <= 0:
        raise ValueError("days must be positive")
    return int(time.time() * 1000) - days * 86_400_000


def validate_candles(candles: list[Candle], interval_ms: int) -> None:
    if interval_ms <= 0:
        raise ValueError("interval_ms must be positive")
    timestamps = [item.timestamp for item in candles]
    if timestamps != sorted(set(timestamps)):
        raise ValueError("candles contain duplicate or out-of-order timestamps")
    gaps = [
        (left, right)
        for left, right in zip(timestamps, timestamps[1:], strict=False)
        if right - left != interval_ms
    ]
    if gaps:
        left, right = gaps[0]
        raise ValueError(f"candle history contains a gap between {left} and {right}")
