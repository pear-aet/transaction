# Execution Engine Acceptance Gates

Execution Engine may not leave draft status until every gate below is satisfied.

## Scope boundary

- Input is a persisted, published and unexpired `RiskDecision`.
- Output is a durable execution plan and order-state history.
- The first implementation is shadow and simulated execution only.
- No exchange write endpoint, trading API key, order submission, amendment or cancellation capability is permitted in this stage.
- Adding a real exchange adapter requires a separate security review and a new explicit delivery stage.

## Decision validity

- Only `RiskDecisionStatus.APPROVED` records may enter execution.
- Decision storage sequence, policy fingerprint, account ID, symbol, action and `valid_until` are revalidated before planning.
- Expired approvals fail closed and cannot be revived by replay.
- Entry plans must preserve approved quantity, direction, reduce-only flag, stop, leverage and margin constraints.
- Exit plans must remain reduce-only and cannot increase exposure.

## Idempotency and identity

- Every execution command has a deterministic command ID derived from the immutable risk decision and execution policy.
- Every client order ID is deterministic, exchange-safe and unique per logical order leg.
- One risk decision cannot create multiple active entry commands.
- Duplicate delivery returns the existing command without rebuilding or resubmitting it.
- Conflicting semantics under one deterministic ID fail closed.

## Order state machine

- State transitions are explicit and monotonic.
- Terminal states cannot return to active states.
- Fill quantity and cumulative quote value never decrease.
- Filled quantity cannot exceed requested quantity.
- Partial fills preserve remaining quantity exactly.
- Cancel and reject transitions retain the complete exchange evidence and reason.
- Unknown exchange states halt the affected command instead of guessing.

## Persistence and outbox

- Command creation, initial order legs and source-decision checkpoint commit atomically.
- Broker dispatch occurs only after persistence.
- A persistent outbox supports restart recovery.
- Delivery state is monotonic and visible.
- Shadow commands are permanently marked non-submitted.
- Pending rows are durable replay barriers and cannot be skipped.

## Simulated execution

- Simulation uses the same command and state contracts as a future real adapter.
- Entry, exit, stop and take-profit legs are explicit.
- Same-bar stop/target ambiguity uses a documented conservative ordering.
- Fees, slippage, minimum quantity, tick size, step size and reduce-only behavior are enforced.
- Partial-fill scenarios, cancellation, rejection and expiry are testable without network access.

## Reconciliation

- Internal command state and broker snapshots are compared by deterministic identifiers.
- Missing internal orders, unexpected broker orders, quantity mismatches and terminal-state conflicts are explicit reconciliation failures.
- Reconciliation never creates or cancels an exchange order implicitly.
- Recovery actions are audit records, not hidden state mutation.

## Delivery and recovery

- Execution consumes RiskDecision through a pinned durable cursor.
- Cursor advancement occurs only after the execution command is durably stored.
- A persisted command with an undispatched outbox entry is recovered without rebuilding the command.
- An expired risk decision is recorded as non-executable and advances only after that disposition is durable.
- Startup subscribes before catch-up to close the live/replay race.

## Observability

- Health exposes received, duplicate, planned, dispatched, acknowledged, partially filled, filled, canceled, rejected, expired and failed counts.
- Queue depth, replay offset, oldest pending age and last reconciliation failure are visible.
- Every state change records command ID, source risk decision ID, timestamp, prior state, new state and evidence.

## Quality and safety gates

- Ruff formatting and linting pass.
- Complete tests pass on Python 3.11 and Python 3.12.
- `pip-audit`, Bandit and Docker build pass.
- No network write method exists in the shadow/simulation stage.
- Tests verify that no module can submit, amend or cancel a real exchange order.
- Security checks may not be disabled or annotated away to force a green build.
