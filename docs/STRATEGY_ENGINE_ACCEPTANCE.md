# Strategy Engine Acceptance Gates

The Strategy Engine is eligible for merge only when all of the following remain true.

## Boundary

- Inputs are persisted, closed `contract` candle events from the Market Engine.
- Outputs are immutable `StrategyIntent` records.
- Strategy output contains no quantity, leverage, margin, order type or exchange-order fields.
- Risk approval, position sizing and execution remain outside this layer.

## Time correctness

- Every strategy decision is bounded by its own `decision_time`.
- A 15-minute decision at an hourly boundary waits for the corresponding closed 1-hour candle.
- Future bars, gaps, revisions and out-of-order history fail closed.
- Realtime evaluation and research adapters call the same pure strategy function.

## Durability

- Evaluation, intent and checkpoint changes are committed atomically.
- Intent identity is deterministic and independent of delivery metadata.
- Published intents have a permanent `storage_sequence`.
- Restart recovery never republishes stale historical entry intents into the live risk path.
- Downstream durable consumers use an idempotent cursor and commit only after successful handling.

## Delivery

- A live intent is considered published only after delivery to a matching critical `BLOCK` subscriber.
- Noncritical dashboard consumers may use replaceable delivery policies explicitly.
- Missing critical delivery leaves the intent pending and raises a visible failure.

## Quality gates

- Ruff formatting and linting pass for `src` and `tests`.
- The complete Pytest suite passes on Python 3.11 and Python 3.12.
- `pip-audit`, Bandit and the Docker build pass.
- The pull request remains draft until all gates are green.
