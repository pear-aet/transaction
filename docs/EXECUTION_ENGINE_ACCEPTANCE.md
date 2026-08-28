# Execution Engine Acceptance Gates

Execution Engine may not leave draft status until every gate below is satisfied.

## 1. Scope boundary

- Input is an immutable, persisted `RiskDecision`.
- Formal execution is allowed only for `RiskDecisionStatus.APPROVED`.
- Output is a durable `ExecutionCommand`, initial order legs and append-only order-state history.
- Supported modes are only `shadow` and deterministic local `simulation`.
- No exchange write endpoint, trading API key, order submission, amendment or cancellation capability is permitted in this stage.
- No broker implementation may contain HTTP, WebSocket, CCXT write or signed exchange-request code.
- Adding a real exchange adapter requires a separate stage, separate security review and explicit user authorization.

## 2. Hard non-submission invariants

- Every command and order contains `submitted=false` and `network_write=false`.
- Every simulated broker update contains `submitted=false` and `network_write=false`.
- Contract construction rejects any attempt to set either safety field to an unsafe value.
- Shadow mode cannot be constructed with a broker.
- Simulation mode requires an explicit local broker.
- `ShadowBroker` has no dispatch or protection-activation capability.
- Health always reports `network_write_enabled=false`.

## 3. RiskDecision provenance

- A decision requires a positive RiskStore `storage_sequence`.
- The complete decision semantics must equal the immutable RiskStore record at that sequence.
- Decision ID, storage sequence, engine instance, policy identity, account, symbol, status and action are revalidated.
- Quantity, reference price, stop, leverage, required margin, reduce-only flag and risk validity are revalidated before dispatch.
- An object that is formally valid but differs from RiskStore fails closed before market lookup or planning.
- Rejected, recorded-only or missing decisions cannot enter execution.

## 4. Decision validity and execution market

- Expired risk approvals fail closed and cannot be revived by replay.
- Execution command validity never extends beyond the source RiskDecision `valid_until`.
- Execution market snapshots cannot come from the future or exceed the policy maximum age.
- Buy planning uses ask and sell planning uses bid; mark is only a controlled fallback.
- Spread and approval-price deviation limits are enforced.
- Entry plans preserve approved quantity, direction, leverage, margin, reduce-only behavior and stop geometry.
- Exit plans remain reduce-only, contain no new stop or margin, and cannot increase exposure.

## 5. Command and order identity

- Every command ID is deterministic from immutable risk and execution-policy semantics.
- Every logical order ID and client order ID is deterministic and unique per command leg.
- Permanent database sequences do not alter command semantics.
- One execution instance and one risk decision produce at most one command.
- One execution instance and one source risk storage sequence produce at most one command.
- Duplicate delivery reuses the stored command without obtaining new market data or rebuilding it.
- Conflicting semantics under the same source decision or deterministic ID fail closed.
- Execution instance account, policy ID, policy version, policy hash and mode are pinned.

## 6. Atomic persistence

- Execution instance, command and all initial order legs commit in one SQLite transaction.
- Broker dispatch occurs only after that transaction is durable and readable.
- Command and order insert collisions are reported as explicit conflicts.
- Current order state and append-only update journal are changed in one transaction.
- A duplicate update ID with identical semantics is idempotent.
- A duplicate update ID with different semantics is rejected.
- Manual direct database mutation is not an accepted recovery method.

## 7. Command outbox

- Shadow commands are stored as `recorded` and never enter broker outbox.
- Simulation commands are stored as `pending` before broker dispatch.
- Delivery states are monotonic:

  ```text
  pending -> dispatched -> completed
  pending -> expired
  pending -> failed
  dispatched -> failed
  ```

- An expired command cannot be dispatched.
- Completion cannot precede dispatch.
- Repeated failure writes cannot change the recorded failure reason.
- Concurrent outbox flushes are serialized and cannot dispatch the same command twice.
- Broker failure leaves the command pending so restart recovery does not rebuild it.

## 8. Upstream publication barrier

- A received RiskDecision may be planned before RiskStore publication finishes, but it cannot be dispatched while the source state is `pending`.
- Source `pending` is a hard outbox barrier.
- Source `published` releases the barrier.
- Source `expired` expires the pending execution command.
- Any other source delivery state fails closed.
- A live-bus race cannot bypass the durable RiskStore source fact.

## 9. Order state machine

- State transitions are explicit and monotonic.
- Terminal states cannot return to active states.
- Fill quantity, cumulative quote value and cumulative fee never decrease.
- Filled quantity cannot exceed requested quantity.
- `partially_filled` must increase cumulative filled quantity.
- Cumulative quote value must match cumulative quantity and average fill price.
- Broker order ID cannot be replaced after it is established.
- Cancellation and rejection retain reason evidence.
- Unsupported transitions halt the affected command instead of guessing.

## 10. Entry protection

- An entry command contains exactly one primary order and one activation-bound protective stop.
- The protective stop is reduce-only and starts as `planned`.
- It activates only after primary partial or full fill.
- Protective activation is deterministic and idempotent.
- Protective rejection, expiry or failure marks the command failed.
- Protective cancellation with existing primary fill marks the command failed.
- An entry command is not complete merely because the primary order filled; protective lifecycle must close the remaining exposure model.

## 11. Local simulated broker

- SimulatedBroker accepts persisted simulation commands only.
- It produces deterministic broker order IDs and update IDs.
- Dispatch creates `dispatched` and `acknowledged` updates without creating a fill.
- Partial fills, full fills, rejection and cancellation are explicit inputs.
- Simulated fills validate quantity, price, fee, time and cumulative totals.
- A simulated fill cannot exceed requested quantity.
- Terminal simulated orders cannot fill again.
- The simulated broker has no network client or credential path.

## 12. Durable RiskDecision runtime

- Execution Runtime subscribes to the critical `BLOCK` bus before catch-up.
- Default durable filter accepts only `PUBLISHED + APPROVED` risk decisions.
- Durable consumer names pin their filters.
- Upstream `pending` records are hard cursor barriers and cannot be skipped.
- Replay batch size remains one to make the persistence/offset failure boundary explicit.
- The upstream cursor advances only after the execution command is durably stored.
- Dispatch failure after command persistence may still advance the upstream cursor because the command is recoverable from the execution outbox.
- Startup recovers pending commands without requesting new execution market data.
- Periodic recovery flushes both execution outbox and upstream durable catch-up.

## 13. Legacy shadow compatibility

- Raw legacy Strategy `Signal` objects are never labeled as risk-approved orders.
- Legacy shadow output reports `risk_approved=false`, `executable=false` and `orders=[]`.
- Formal shadow reports accept persisted shadow `ExecutionCommand` records only.
- Formal shadow plans preserve approved quantity, order legs, client order IDs and source decision identity.
- Neither legacy nor formal shadow reporting can dispatch a broker operation.

## 14. Reconciliation

- Reconciliation input represents a complete broker order ledger snapshot for one command.
- Comparison uses deterministic client order IDs.
- Exact matches create no updates.
- Valid forward progress is converted into deterministic OrderUpdate records.
- Missing active local orders are explicit failures.
- Unknown broker orders are explicit failures.
- Broker appearance of a locally planned order is explicit unexpected activation.
- Broker order ID mismatch is an explicit failure.
- State, fill, quote or fee regression is an explicit failure.
- Any reconciliation issue marks the command failed before raising `ExecutionReconciliationError`.
- Reconciliation never implicitly creates, modifies or cancels an order.
- Reconciliation evidence remains `submitted=false` and `network_write=false`.

## 15. Observability

- Health exposes received and duplicate decisions.
- Health exposes planned, persisted, shadow-recorded, pending, dispatched, completed, expired and failed commands.
- Health exposes order-update and protective-activation counts.
- Health exposes reconciliation runs, reconciliation updates and reconciliation failures.
- Store metrics expose command and order states, update count and latest command sequence.
- Runtime health exposes replayed decisions, skipped records, periodic flushes, source barriers and upstream offset.
- Oldest pending command age and latest error are visible.

## 16. Documentation and operational safety

- Architecture documentation places Execution Runtime, Engine, Store and local brokers in the implemented chain.
- Operations documentation covers startup order, outbox recovery, publication barriers, protection failures, reconciliation, backup and forbidden actions.
- README states the implemented shadow/simulation scope and still declares that real trading is unavailable.
- Documentation does not describe passing tests as authorization to use real funds.

## 17. Quality gates

- Ruff formatting and linting pass for all `src` and `tests`.
- Complete Pytest suite passes on Python 3.11 and Python 3.12.
- Targeted contract, store, broker, engine, runtime, shadow and reconciliation tests pass.
- `pip-audit` reports no known dependency vulnerabilities.
- Bandit medium/high severity scan passes.
- Docker image build passes.
- No acceptance gate is bypassed by weakening tests, disabling a security check or annotating away a valid finding.

## 18. Explicitly unfinished

This stage does not authorize live trading. The following remain separate future work:

- real exchange read-only account and order adapters;
- real order submission, amendment and cancellation;
- least-privilege credential storage;
- exchange time synchronization, signatures, rate limits and retry policy;
- user-data stream and REST dual-channel reconciliation;
- balance and position reconciliation;
- unknown-order quarantine and operator approval;
- independent Portfolio Engine;
- emergency close and manual intervention workflow;
- long-running shadow validation and live-execution security review.
