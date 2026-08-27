# Market Engine

Market Engine is the only supported path from exchange market data to strategy, risk, portfolio and monitoring consumers.

```text
Binance WebSocket
       |
       v
BinanceFuturesStream
       |
       v
MarketDataEngine validation, persistence and gap repair
       |
       v
Canonical MarketEvent
       |
       v
MarketEventBus
       |
       +--> strategy subscriber
       +--> risk subscriber
       +--> portfolio subscriber
       +--> dashboard subscriber
```

## Non-negotiable invariants

1. A closed-candle event is published only after the candle passes validation and persistence.
2. Unfinished candles never enter the bus.
3. Symbols are converted to one canonical CCXT form before publication. Unknown symbols fail closed by default.
4. Every accepted event receives one process-local, monotonically increasing sequence number.
5. Exact duplicate events are suppressed by a bounded deterministic event-ID cache.
6. Candle revisions are not mistaken for duplicates because OHLCV values are part of event identity.
7. One publication lock preserves the same global order for every subscriber.
8. Backpressure is selected explicitly for every subscriber; there is no implicit silent dropping.
9. Storage, normalization and publication failures remain visible and stop that event from reaching consumers.
10. Blocking CCXT and SQLite work runs outside the asyncio event-loop thread.
11. Strategy and risk code must subscribe to this bus instead of opening exchange connections.

## Event types

### `candle_closed`

Contains the canonical symbol, timeframe, candle start time, exchange event time, local receipt time, price type and complete OHLCV values.

The default price type is `contract`. Mark-price candles remain separately stored by the Data Engine and can later be exposed as another event stream without changing the envelope.

### `mark_price`

Contains mark price, index price, current funding rate and next funding timestamp. Mark-price updates have no candle timeframe.

Every event carries `schema_version=1`. A future incompatible payload change must increase that version instead of silently changing consumer expectations.

## Event identity and sequence

`event_id` answers whether two inputs represent the same exact exchange fact. It is deterministic across process restarts.

`sequence` answers where an event was published inside the current process. It starts at one and is monotonic, but it is not a durable database offset. The bus rejects events that already carry a sequence so stale events cannot be accidentally republished as new facts.

Consumers should retain `event_id` in state transitions that must be idempotent. A sequence gap after cancelled publication is detectable and must not be interpreted as missing exchange data without checking persisted candles.

## Backpressure policies

### `block`

The default for strategy, risk and portfolio subscribers. Once the subscriber queue reaches capacity, publication waits until the subscriber consumes an event. This preserves every event but allows one critical slow consumer to slow the ordered pipeline.

### `drop_oldest`

For dashboards and replaceable telemetry only. When full, the oldest queued event is removed so the newest state remains visible. It must not be used for order generation, position accounting or risk decisions.

### `disconnect`

For non-critical consumers that must never delay trading logic. A full queue closes and removes that subscriber. Already queued events can still be drained before iteration ends.

## Startup and recovery

`MarketRuntime.bootstrap()` incrementally synchronizes contract candles, mark-price candles and funding history before real-time processing begins. Any closed candle arriving after bootstrap is checked against the latest stored timestamp. A detected interval gap is repaired through REST before the new live candle is persisted and published.

Bootstrap and gap repair call synchronous data adapters through worker threads. This prevents network or SQLite latency from blocking WebSocket ping/pong handling.

The restart sequence is:

1. Load existing SQLite market-data state.
2. Incrementally bootstrap configured symbols and timeframes.
3. Start the WebSocket stream.
4. Repair any bootstrap-to-stream gap on the first later closed candle.
5. Publish only after persistence succeeds.

## Health contract

`MarketRuntime.health_snapshot()` includes four independently inspectable sections:

- runtime counters, blocking-call count and last failure;
- WebSocket connection and reconnect state;
- Data Engine synchronization, rejection and repair state;
- Event Bus sequence, latency, duplicate, backpressure and per-subscriber queue state.

A production supervisor should halt new trade decisions when the runtime is not running, the stream is stale, publication failures increase, or a required critical subscriber disappears.

## Current boundary

This layer does not calculate indicators, generate signals, size positions or place orders. Those responsibilities belong to later layers and must consume the canonical events defined here.
