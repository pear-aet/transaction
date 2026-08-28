# Risk Engine Acceptance Gates

The Risk Engine is eligible for merge only when every gate below remains true.

## Boundary

- Input is a persisted `StrategyIntent` plus immutable account and market snapshots.
- Output is an immutable approved or rejected `RiskDecision`.
- The layer may approve quantity, leverage, required margin and reduce-only behavior.
- The layer must not submit, amend or cancel exchange orders.
- A strategy cannot override account, portfolio, correlation-group, market-rule or liquidation limits.
- Every evaluator result is validated against the exact engine instance, policy, account snapshot, market snapshot and strategy intent before persistence.

## Time and snapshot correctness

- Future strategy intents and future account or market snapshots fail closed.
- Entry intents, account snapshots and market snapshots have explicit maximum ages.
- Every approved decision carries a deterministic `valid_until` boundary.
- An expired approval cannot be published or restored to a pending state.
- Risk-reducing exits may bypass account halts and stale account or market snapshots, but may not bypass a stale or future strategy intent.
- Startup cleanup expires stale pending approvals before the absence of a live execution subscriber can stop the runtime.

## Sizing and leverage

- Risk per unit includes adverse entry slippage, stop distance and round-trip fees.
- Quantity is rounded down to the exchange step size and never exceeds the approved risk budget.
- Stop prices are rounded in the conservative direction.
- Symbol, portfolio, correlation-group, gross-notional and directional-notional limits are enforced independently.
- Dynamic leverage is capped by policy, exchange rules, volatility, stop geometry, available balance and cross-margin liquidation buffer.
- Maintenance-margin brackets are continuous, ordered and validated before approval.
- Exchange minimum quantity and notional rules fail closed.
- Approved notional equals quantity multiplied by approval price.
- Approved margin cannot be below notional divided by leverage.
- Approved initial risk cannot exceed the approved budget.
- Post-approval risk and notional fields must include the new position.
- Approved exits are reduce-only, use leverage one and introduce no new margin or risk.

## Durability and idempotency

- One risk-engine instance and one strategy intent can produce only one immutable decision.
- Account and policy identity are pinned to the risk-engine instance.
- Decision and evaluation records are committed atomically.
- Approved decisions enter a persistent `pending` outbox; rejected decisions are audit-only `recorded` facts.
- Delivery state is monotonic: `pending -> published` or `pending -> expired`.
- Duplicate strategy intents reuse the stored decision without obtaining new snapshots or recalculating risk.
- A custom evaluator cannot substitute a decision belonging to another intent, account, market snapshot or policy.

## Delivery and recovery

- Only persisted, approved, unexpired and unsequenced decisions may enter the live decision bus.
- A live approval requires delivery to a matching critical subscriber using blocking backpressure.
- Missing execution delivery leaves the approval pending and raises a visible failure.
- Startup recovery publishes still-valid pending approvals and expires stale approvals instead of replaying them.
- Durable execution cursors expose published approvals by default; rejected and expired decisions require an explicit audit filter.
- `pending` rows are durable cursor barriers. A consumer must stop before them and may never count them as skipped or advance to later terminal rows.
- Durable filters cannot include mutable `pending` states.
- Durable cursor filters are pinned and stale or out-of-snapshot batches cannot commit.
- The Strategy-to-Risk runtime subscribes before startup catch-up and advances its upstream cursor only after the risk decision is durably stored.
- The same pending-barrier rule applies to upstream StrategyIntent replay, preventing an unpublished intent from being permanently skipped.

## Compatibility and quality

- Legacy `EventEngine` entry sizing must be migrated to or parity-tested against the same pure risk calculation before this PR leaves draft status.
- Ruff formatting and linting pass for `src` and `tests`.
- The complete Pytest suite passes on Python 3.11 and Python 3.12.
- `pip-audit`, Bandit and the Docker build pass.
- No acceptance gate may be bypassed by weakening tests, disabling security checks or annotating away a valid finding.
