# Data Engine

The Data Engine is the only market-data gateway that strategy, risk and portfolio code may consume.

## Guarantees

1. Strategy-visible K-lines are closed bars only.
2. K-lines are ordered, unique and contiguous at the configured interval.
3. Prices must be finite and positive; volume must be finite and non-negative.
4. Historical synchronization is incremental and resumes after the last persisted closed bar.
5. A live-bar gap is repaired from the REST history endpoint before the new live bar is accepted.
6. Replayed/duplicate closed-bar messages are idempotent at persistence.
7. Funding history is timestamp-deduplicated and incrementally resumable.
8. Mark price, index price, current funding rate and next funding time are represented as typed live events.
9. WebSocket transport reconnects with bounded exponential backoff; transport health is observable.
10. Permanent request/configuration failures are not hidden behind repeated retries.

## Current authoritative sources

For Binance USD-M perpetual research:

- Contract OHLCV: Binance futures market data through CCXT.
- Mark-price OHLCV: Binance mark-price K-line endpoint through CCXT.
- Funding history: Binance funding-rate history through CCXT.
- Trading filters: Binance exchange information through CCXT market metadata.
- Leverage tiers: authenticated read-only leverage-tier endpoint when credentials are available; otherwise a conservative research fallback remains explicit.
- Live contract K-lines: Binance USD-M combined WebSocket streams.
- Live mark/index price and funding snapshot: Binance USD-M mark-price WebSocket stream.

## Closed-bar rule

Binance streams publish updates while the current K-line is still forming. The parser drops every K-line where the exchange close flag is false. Only the final closed K-line can enter `MarketDataEngine`.

This rule prevents look-ahead-like behavior in the live system and keeps live signal timing consistent with backtests that operate on closed bars.

## Gap repair

Given a persisted last bar at `T` and a new live bar at `T + n * interval`, where `n > 1`:

1. Do not persist the new live bar yet.
2. Request the exact missing interval `[T + interval, new_bar - interval]` over REST.
3. Validate ordering, uniqueness, numerical validity and continuity.
4. Persist the repaired interval transactionally through the repository upsert path.
5. Verify the expected number of timestamps exists.
6. Only then persist the new live bar.

If repair is incomplete, ingestion raises an error and the new bar does not advance the canonical series.

## Health

`StreamHealth` tracks connection state, reconnect count, last message time, latest closed-bar timestamps and the last transport error.

`DataEngineHealth` tracks last historical synchronization, last accepted live event, repaired gaps, rejected events and the last ingestion error.

These health snapshots will later feed the monitoring/circuit-breaker layer. A stale or unhealthy Data Engine must eventually block new trading decisions rather than allow a strategy to act on old data.

## Scope discipline

The core feed deliberately does not collect every exchange metric merely because it exists. Open interest, long/short ratios, liquidation streams, order-book depth and trade-by-trade data should be introduced only when a strategy or risk model has a defined hypothesis and retention requirement for them.

This keeps the canonical execution feed small, deterministic and auditable while leaving room for separate research feeds.

## Acceptance tests

The first-stage Data Engine is accepted only when CI validates at least:

- REST pagination and open-bar filtering.
- Candle gap rejection.
- Funding pagination.
- Exchange filter/leverage-tier parsing.
- WebSocket open-bar rejection and closed-bar parsing.
- Combined stream construction.
- Incremental historical resume.
- Live gap repair before persistence.
- Duplicate live event idempotency.
- Incremental funding resume.
- Mark-price snapshot ingestion.

A public Binance end-to-end smoke test still requires a deployment network that can reach Binance futures endpoints. Unit/CI success alone is not evidence of live connectivity.
