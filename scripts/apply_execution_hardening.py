from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


def append_once(path: str, marker: str, content: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if marker in text:
        return
    file.write_text(text.rstrip() + "\n\n" + content.lstrip(), encoding="utf-8")


STORE = "src/crypto_quant/execution_store.py"
ENGINE = "src/crypto_quant/execution_engine.py"
RECOVERY_TEST = "tests/test_execution_recovery.py"
ENGINE_TEST = "tests/test_execution_engine.py"
STORE_TEST = "tests/test_execution_store.py"
RISK_TEST = "tests/test_risk_final_recovery.py"

replace_once(
    STORE,
    "from pathlib import Path\n\nfrom .execution import apply_order_update\n",
    "from pathlib import Path\nfrom typing import Any\n\nfrom .execution import apply_order_update\n",
)
replace_once(
    STORE,
    "    ExecutionOrder,\n    OrderState,\n",
    "    ExecutionOrder,\n    OrderLegType,\n    OrderState,\n",
)
replace_once(
    STORE,
    '''@dataclass(frozen=True, slots=True)
class StoredOrderUpdate:
    update: OrderUpdate
    storage_sequence: int
    prior_state: OrderState
    new_state: OrderState


class ExecutionStore:
''',
    '''@dataclass(frozen=True, slots=True)
class StoredOrderUpdate:
    update: OrderUpdate
    storage_sequence: int
    prior_state: OrderState
    new_state: OrderState


@dataclass(frozen=True, slots=True)
class StoredCommandTransition:
    storage_sequence: int
    command_id: str
    source_decision_id: str
    event_time: int
    prior_state: ExecutionDeliveryState
    new_state: ExecutionDeliveryState
    reason: str | None
    evidence: dict[str, Any]


class ExecutionStore:
''',
)
replace_once(
    STORE,
    '''                CREATE INDEX IF NOT EXISTS execution_updates_order_sequence
                    ON execution_order_updates(order_id, storage_sequence);
                """
''',
    '''                CREATE INDEX IF NOT EXISTS execution_updates_order_sequence
                    ON execution_order_updates(order_id, storage_sequence);

                CREATE TABLE IF NOT EXISTS execution_command_transitions (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL,
                    source_decision_id TEXT NOT NULL,
                    event_time INTEGER NOT NULL CHECK(event_time >= 0),
                    prior_state TEXT NOT NULL,
                    new_state TEXT NOT NULL,
                    reason TEXT,
                    evidence TEXT NOT NULL,
                    UNIQUE(command_id, prior_state, new_state),
                    FOREIGN KEY(command_id)
                        REFERENCES execution_commands(command_id)
                );
                CREATE INDEX IF NOT EXISTS execution_transitions_command_sequence
                    ON execution_command_transitions(command_id, storage_sequence);
                """
''',
)
replace_once(
    STORE,
    '''            evidence=dict(value.get("evidence") or {}),
        )

    @staticmethod
    def _expected_delivery_state(command: ExecutionCommand) -> ExecutionDeliveryState:
''',
    '''            evidence=dict(value.get("evidence") or {}),
        )

    @staticmethod
    def _encode_evidence(evidence: dict[str, Any] | None) -> str:
        return json.dumps(
            evidence or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _expected_delivery_state(command: ExecutionCommand) -> ExecutionDeliveryState:
''',
)
replace_once(
    STORE,
    '''        timestamp: int,
        reason: str | None = None,
    ) -> bool:
        if not command_id or timestamp < 0:
            raise ValueError("invalid execution delivery transition")
        with self._lock, self._connect() as connection:
''',
    '''        timestamp: int,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        if not command_id or timestamp < 0:
            raise ValueError("invalid execution delivery transition")
        encoded_evidence = self._encode_evidence(evidence)
        with self._lock, self._connect() as connection:
''',
)
replace_once(
    STORE,
    '''            if target is ExecutionDeliveryState.FAILED:
                if not reason:
                    raise ValueError("failed execution command requires a reason")
                if stored.dispatched_at is not None and timestamp < stored.dispatched_at:
                    raise ValueError("execution failure cannot precede dispatch")

            if target is ExecutionDeliveryState.DISPATCHED:
''',
    '''            if target is ExecutionDeliveryState.FAILED:
                if not reason:
                    raise ValueError("failed execution command requires a reason")
                if stored.dispatched_at is not None and timestamp < stored.dispatched_at:
                    raise ValueError("execution failure cannot precede dispatch")

            orders = stored.orders
            primary = [order for order in orders if order.leg_type is OrderLegType.PRIMARY]
            protective = [
                order for order in orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
            ]
            if target is ExecutionDeliveryState.DISPATCHED:
                if len(primary) != 1 or primary[0].state not in {
                    OrderState.DISPATCHED,
                    OrderState.ACKNOWLEDGED,
                    OrderState.PARTIALLY_FILLED,
                    OrderState.FILLED,
                }:
                    raise ValueError("dispatch requires a submitted primary order")
            elif target is ExecutionDeliveryState.COMPLETED:
                if len(primary) != 1 or primary[0].state is not OrderState.FILLED:
                    raise ValueError("completion requires a filled primary order")
                if protective and any(
                    order.state is not OrderState.FILLED for order in protective
                ):
                    raise ValueError("entry completion requires filled protective closure")
            elif target is ExecutionDeliveryState.EXPIRED:
                if any(order.state is not OrderState.PLANNED for order in orders):
                    raise ValueError("command with submitted orders cannot expire")

            if target is ExecutionDeliveryState.DISPATCHED:
''',
)
replace_once(
    STORE,
    '''            else:
                query = """UPDATE execution_commands SET
                    delivery_state='failed',failed_at=?,failure_reason=? WHERE command_id=?"""
                parameters = (timestamp, reason, command_id)
            connection.execute(query, parameters)
        return True

    def mark_dispatched(self, command_id: str, dispatched_at: int) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset({ExecutionDeliveryState.PENDING}),
            target=ExecutionDeliveryState.DISPATCHED,
            timestamp=dispatched_at,
        )

    def mark_completed(self, command_id: str, completed_at: int) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset({ExecutionDeliveryState.DISPATCHED}),
            target=ExecutionDeliveryState.COMPLETED,
            timestamp=completed_at,
        )

    def mark_expired(self, command_id: str, expired_at: int) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset({ExecutionDeliveryState.PENDING}),
            target=ExecutionDeliveryState.EXPIRED,
            timestamp=expired_at,
        )

    def mark_failed(self, command_id: str, failed_at: int, reason: str) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset(
                {
                    ExecutionDeliveryState.PENDING,
                    ExecutionDeliveryState.DISPATCHED,
                }
            ),
            target=ExecutionDeliveryState.FAILED,
            timestamp=failed_at,
            reason=reason,
        )
''',
    '''            else:
                query = """UPDATE execution_commands SET
                    delivery_state='failed',failed_at=?,failure_reason=? WHERE command_id=?"""
                parameters = (timestamp, reason, command_id)
            connection.execute(query, parameters)
            try:
                connection.execute(
                    """INSERT INTO execution_command_transitions(
                        command_id,source_decision_id,event_time,prior_state,
                        new_state,reason,evidence
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        command_id,
                        stored.command.source_decision_id,
                        timestamp,
                        state.value,
                        target.value,
                        reason,
                        encoded_evidence,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ExecutionCommandConflict(
                    f"execution transition collision: {command_id}"
                ) from exc
        return True

    def mark_dispatched(
        self,
        command_id: str,
        dispatched_at: int,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset({ExecutionDeliveryState.PENDING}),
            target=ExecutionDeliveryState.DISPATCHED,
            timestamp=dispatched_at,
            evidence=evidence,
        )

    def mark_completed(
        self,
        command_id: str,
        completed_at: int,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset({ExecutionDeliveryState.DISPATCHED}),
            target=ExecutionDeliveryState.COMPLETED,
            timestamp=completed_at,
            evidence=evidence,
        )

    def mark_expired(
        self,
        command_id: str,
        expired_at: int,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset({ExecutionDeliveryState.PENDING}),
            target=ExecutionDeliveryState.EXPIRED,
            timestamp=expired_at,
            evidence=evidence,
        )

    def mark_failed(
        self,
        command_id: str,
        failed_at: int,
        reason: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        return self._transition_delivery(
            command_id,
            expected=frozenset(
                {
                    ExecutionDeliveryState.PENDING,
                    ExecutionDeliveryState.DISPATCHED,
                }
            ),
            target=ExecutionDeliveryState.FAILED,
            timestamp=failed_at,
            reason=reason,
            evidence=evidence,
        )
''',
)
replace_once(
    STORE,
    '''            current = self._decode_order(str(order_row["payload"]))
            updated = apply_order_update(current, update)
''',
    '''            current = self._decode_order(str(order_row["payload"]))
            command_row = connection.execute(
                "SELECT delivery_state FROM execution_commands WHERE command_id=?",
                (current.command_id,),
            ).fetchone()
            if command_row is None:
                raise RuntimeError("execution order exists without its parent command")
            command_state = ExecutionDeliveryState(command_row["delivery_state"])
            if command_state not in {
                ExecutionDeliveryState.PENDING,
                ExecutionDeliveryState.DISPATCHED,
            }:
                raise ExecutionUpdateConflict(
                    f"new order update cannot target {command_state.value} command"
                )
            if command_state is ExecutionDeliveryState.PENDING:
                if update.new_state not in {
                    OrderState.DISPATCHED,
                    OrderState.ACKNOWLEDGED,
                } or update.cumulative_filled_quantity != 0:
                    raise ExecutionUpdateConflict(
                        "pending command accepts dispatch and acknowledgement updates only"
                    )
            if (
                current.leg_type is OrderLegType.PROTECTIVE_STOP
                and update.cumulative_filled_quantity > 0
            ):
                primary_row = connection.execute(
                    """SELECT payload FROM execution_orders
                    WHERE command_id=? AND leg_type='primary'""",
                    (current.command_id,),
                ).fetchone()
                if primary_row is None:
                    raise RuntimeError("protective order has no primary order")
                primary = self._decode_order(str(primary_row["payload"]))
                if (
                    update.cumulative_filled_quantity
                    > primary.cumulative_filled_quantity + 1e-12
                ):
                    raise ExecutionUpdateConflict(
                        "protective fill exceeds primary filled exposure"
                    )
            updated = apply_order_update(current, update)
''',
)
replace_once(
    STORE,
    '''    def latest_command_sequence(self) -> int:
''',
    '''    def active_commands(
        self,
        *,
        engine_instance_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[StoredExecutionCommand]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("command limit must be in [1, 10000]")
        query = """SELECT * FROM execution_commands
        WHERE delivery_state='dispatched'
        AND storage_sequence>?
        AND (? IS NULL OR engine_instance_id=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (after_sequence, engine_instance_id, engine_instance_id, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._stored_from_row(connection, row) for row in rows]

    def command_transitions(self, command_id: str) -> list[StoredCommandTransition]:
        if not command_id:
            raise ValueError("command_id must not be empty")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM execution_command_transitions
                WHERE command_id=? ORDER BY storage_sequence""",
                (command_id,),
            ).fetchall()
        return [
            StoredCommandTransition(
                storage_sequence=int(row["storage_sequence"]),
                command_id=str(row["command_id"]),
                source_decision_id=str(row["source_decision_id"]),
                event_time=int(row["event_time"]),
                prior_state=ExecutionDeliveryState(row["prior_state"]),
                new_state=ExecutionDeliveryState(row["new_state"]),
                reason=None if row["reason"] is None else str(row["reason"]),
                evidence=dict(json.loads(str(row["evidence"]))),
            )
            for row in rows
        ]

    def latest_command_sequence(self) -> int:
''',
)
replace_once(
    STORE,
    '''            updates = int(
                connection.execute("SELECT count(*) FROM execution_order_updates").fetchone()[0]
            )
            latest = int(
''',
    '''            updates = int(
                connection.execute("SELECT count(*) FROM execution_order_updates").fetchone()[0]
            )
            transitions = int(
                connection.execute(
                    "SELECT count(*) FROM execution_command_transitions"
                ).fetchone()[0]
            )
            latest = int(
''',
)
replace_once(
    STORE,
    '''            "order_updates": updates,
            "latest_command_sequence": latest,
''',
    '''            "order_updates": updates,
            "command_transitions": transitions,
            "latest_command_sequence": latest,
''',
)

replace_once(
    ENGINE,
    '''class ExecutionBootstrapResult:
    dispatched: int
    expired: int
    blocked_on_unpublished_source: int
    remaining_pending: int
''',
    '''class ExecutionBootstrapResult:
    dispatched: int
    expired: int
    blocked_on_unpublished_source: int
    remaining_pending: int
    reconciled: int = 0
''',
)
replace_once(
    ENGINE,
    '''    protection_activations: int = 0
    failures: int = 0
''',
    '''    protection_activations: int = 0
    startup_reconciliations: int = 0
    failures: int = 0
''',
)
replace_once(
    ENGINE,
    '''            "protection_activations": self.protection_activations,
            "failures": self.failures,
''',
    '''            "protection_activations": self.protection_activations,
            "startup_reconciliations": self.startup_reconciliations,
            "failures": self.failures,
''',
)
replace_once(
    ENGINE,
    '''                changed = await asyncio.to_thread(
                    self.store.mark_failed,
                    stored.command.command_id,
                    now_ms,
                    failure_reason,
                )
''',
    '''                changed = await asyncio.to_thread(
                    self.store.mark_failed,
                    stored.command.command_id,
                    now_ms,
                    failure_reason,
                    evidence={"source": "order_reconciliation"},
                )
''',
)
replace_once(
    ENGINE,
    '''            changed = await asyncio.to_thread(
                self.store.mark_completed,
                stored.command.command_id,
                now_ms,
            )
''',
    '''            changed = await asyncio.to_thread(
                self.store.mark_completed,
                stored.command.command_id,
                now_ms,
                evidence={"source": "order_reconciliation"},
            )
''',
)
replace_once(
    ENGINE,
    '''        command = stored.command
        if now_ms > command.valid_until:
            changed = await asyncio.to_thread(
                self.store.mark_expired,
                command.command_id,
                now_ms,
            )
''',
    '''        command = stored.command
        submitted_orders = [
            order for order in stored.orders if order.state is not OrderState.PLANNED
        ]
        if submitted_orders:
            recovered_dispatch_time = max(order.updated_at for order in submitted_orders)
            if recovered_dispatch_time > command.valid_until:
                raise ExecutionDispatchError(
                    "persisted order update occurred after command validity"
                )
            changed = await asyncio.to_thread(
                self.store.mark_dispatched,
                command.command_id,
                recovered_dispatch_time,
                evidence={"source": "pending_order_recovery"},
            )
            if changed:
                self.health.commands_dispatched += 1
            refreshed = await asyncio.to_thread(
                self.store.load_command,
                command.command_id,
            )
            if refreshed is None:
                raise RuntimeError("execution command disappeared during dispatch recovery")
            await self._reconcile_command(refreshed, now_ms)
            return True
        if now_ms > command.valid_until:
            changed = await asyncio.to_thread(
                self.store.mark_expired,
                command.command_id,
                now_ms,
                evidence={"source": "outbox_expiry"},
            )
''',
)
replace_once(
    ENGINE,
    '''            changed = await asyncio.to_thread(
                self.store.mark_dispatched,
                command.command_id,
                now_ms,
            )
''',
    '''            changed = await asyncio.to_thread(
                self.store.mark_dispatched,
                command.command_id,
                now_ms,
                evidence={"source": "local_simulation_broker"},
            )
''',
)
replace_once(
    ENGINE,
    '''    async def _flush_outbox_unlocked(
''',
    '''    async def _reconcile_active_commands(self, now_ms: int) -> int:
        reconciled = 0
        after_sequence = 0
        while True:
            active = await asyncio.to_thread(
                self.store.active_commands,
                engine_instance_id=self.settings.engine_instance_id,
                after_sequence=after_sequence,
                limit=self.settings.outbox_batch_size,
            )
            if not active:
                break
            for stored in active:
                await self._reconcile_command(stored, now_ms)
                reconciled += 1
                sequence = stored.command.storage_sequence
                if sequence is None:
                    raise RuntimeError("active execution command has no storage sequence")
                after_sequence = sequence
            if len(active) < self.settings.outbox_batch_size:
                break
        self.health.startup_reconciliations += reconciled
        return reconciled

    async def _flush_outbox_unlocked(
''',
)
replace_once(
    ENGINE,
    '''            try:
                result = await self._flush_outbox_unlocked(now_ms=current)
                self.health.bootstrapped = True
                self.health.bootstrap_runs += 1
                self.health.last_error = None
                return result
''',
    '''            try:
                result = await self._flush_outbox_unlocked(now_ms=current)
                reconciled = await self._reconcile_active_commands(current)
                self.health.bootstrapped = True
                self.health.bootstrap_runs += 1
                self.health.last_error = None
                return ExecutionBootstrapResult(
                    result.dispatched,
                    result.expired,
                    result.blocked_on_unpublished_source,
                    result.remaining_pending,
                    reconciled,
                )
''',
)

replace_once(RISK_TEST, "        leverage=1,\n", "        effective_leverage=1,\n")

replace_once(
    STORE_TEST,
    "from crypto_quant.execution import build_execution_command\n",
    "from crypto_quant.execution import build_execution_command\nfrom crypto_quant.execution_broker import SimulatedBroker\n",
)
replace_once(
    STORE_TEST,
    '''    assert store.metrics() == {
        "instances": 1,
''',
    '''    assert store.metrics() == {
        "instances": 1,
''',
)
replace_once(
    STORE_TEST,
    '''        "order_updates": 0,
        "latest_command_sequence": 1,
''',
    '''        "order_updates": 0,
        "command_transitions": 0,
        "latest_command_sequence": 1,
''',
)
replace_once(
    STORE_TEST,
    '''def test_simulation_outbox_and_delivery_transitions_are_monotonic(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "quant.db")
    value = command(policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION))
    commit = store.commit_command(value, updated_at=NOW)

    assert commit.stored_command.delivery_state is ExecutionDeliveryState.PENDING
    assert [item.command.command_id for item in store.pending_commands()] == [value.command_id]
    assert store.oldest_pending_created_at() == NOW
    assert store.mark_dispatched(value.command_id, NOW + 1) is True
    assert store.mark_dispatched(value.command_id, NOW + 2) is False
    assert store.mark_completed(value.command_id, NOW + 2) is True
    assert store.mark_completed(value.command_id, NOW + 3) is False
    with pytest.raises(ValueError, match="cannot move"):
        store.mark_failed(value.command_id, NOW + 4, "late_failure")
''',
    '''def test_simulation_outbox_and_delivery_transitions_are_monotonic(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "quant.db")
    value = command(policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION))
    stored = store.commit_command(value, updated_at=NOW).stored_command
    broker = SimulatedBroker()

    assert stored.delivery_state is ExecutionDeliveryState.PENDING
    assert [item.command.command_id for item in store.pending_commands()] == [value.command_id]
    assert store.oldest_pending_created_at() == NOW
    for update in broker.dispatch(stored.command, stored.orders, NOW + 1):
        store.apply_order_update(update)
    assert store.mark_dispatched(value.command_id, NOW + 1) is True
    assert store.mark_dispatched(value.command_id, NOW + 2) is False

    current = store.load_command(value.command_id)
    assert current is not None
    primary = current.orders[0]
    store.apply_order_update(
        broker.fill(
            primary,
            fill_quantity=primary.quantity,
            fill_price=100.1,
            fee=0.1,
            event_time=NOW + 2,
        )
    )
    current = store.load_command(value.command_id)
    assert current is not None
    for update in broker.activate_protection(current.command, current.orders, NOW + 2):
        store.apply_order_update(update)
    current = store.load_command(value.command_id)
    assert current is not None
    protective = current.orders[1]
    store.apply_order_update(
        broker.fill(
            protective,
            fill_quantity=protective.quantity,
            fill_price=99,
            fee=0.1,
            event_time=NOW + 3,
        )
    )
    assert store.mark_completed(value.command_id, NOW + 3) is True
    assert store.mark_completed(value.command_id, NOW + 4) is False
    with pytest.raises(ValueError, match="cannot move"):
        store.mark_failed(value.command_id, NOW + 5, "late_failure")

    transitions = store.command_transitions(value.command_id)
    assert [item.new_state for item in transitions] == [
        ExecutionDeliveryState.DISPATCHED,
        ExecutionDeliveryState.COMPLETED,
    ]
    assert all(item.source_decision_id == value.source_decision_id for item in transitions)
''',
)
replace_once(
    STORE_TEST,
    '''    store.apply_order_update(acknowledged)
    quantity = primary.quantity / 2
''',
    '''    store.apply_order_update(acknowledged)
    store.mark_dispatched(value.command_id, NOW + 2)
    quantity = primary.quantity / 2
''',
)

replace_once(
    RECOVERY_TEST,
    '''from crypto_quant.execution_contracts import (
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
)
''',
    '''from crypto_quant.execution_broker import SimulatedBroker
from crypto_quant.execution_contracts import (
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
    OrderState,
)
''',
)
replace_once(
    RECOVERY_TEST,
    "from crypto_quant.execution_store import ExecutionStore\n",
    "from crypto_quant.execution_store import ExecutionDeliveryState, ExecutionStore\n",
)
replace_once(
    RECOVERY_TEST,
    '''        await engine.bootstrap(now_ms=NOW)

        assert len(calls) == 1
''',
    '''        result = await engine.bootstrap(now_ms=NOW)

        assert len(calls) == 1
        assert result.reconciled == 1
''',
)
append_once(
    RECOVERY_TEST,
    "test_pending_acknowledged_order_recovers_after_command_ttl",
    '''def test_pending_acknowledged_order_recovers_after_command_ttl(tmp_path) -> None:
    async def scenario() -> None:
        database = tmp_path / "quant.db"
        risk_store = RiskStore(database)
        decision = persisted_decision(risk_store)
        execution_store = ExecutionStore(database)
        policy = ExecutionPolicy(mode=ExecutionMode.SIMULATION)
        command = build_execution_command(
            ExecutionContext(
                "execution-primary",
                decision,
                execution_market(decision, NOW),
                policy,
                NOW,
            )
        )
        stored = execution_store.commit_command(command, updated_at=NOW).stored_command
        broker = SimulatedBroker()
        for update in broker.dispatch(stored.command, stored.orders, NOW + 1):
            execution_store.apply_order_update(update)
        pending = execution_store.load_command(command.command_id)
        assert pending is not None
        assert pending.delivery_state is ExecutionDeliveryState.PENDING
        assert pending.orders[0].state is OrderState.ACKNOWLEDGED

        engine = ExecutionEngine(
            ExecutionEngineSettings("execution-primary", "paper"),
            policy,
            execution_market,
            store=execution_store,
            risk_store=risk_store,
            broker=broker,
        )
        result = await engine.bootstrap(now_ms=command.valid_until + 1)
        recovered = execution_store.load_command(command.command_id)

        assert result.dispatched == 1
        assert recovered is not None
        assert recovered.delivery_state is ExecutionDeliveryState.DISPATCHED
        transitions = execution_store.command_transitions(command.command_id)
        assert transitions[0].evidence == {"source": "pending_order_recovery"}

    asyncio.run(scenario())
''',
)

replace_once(
    ENGINE_TEST,
    "from crypto_quant.execution_store import ExecutionDeliveryState, ExecutionStore\n",
    '''from crypto_quant.execution_store import (
    ExecutionDeliveryState,
    ExecutionStore,
    ExecutionUpdateConflict,
)
''',
)
append_once(
    ENGINE_TEST,
    "test_protective_fill_cannot_exceed_primary_filled_exposure",
    '''def test_protective_fill_cannot_exceed_primary_filled_exposure(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_risk_decision(risk_store, published=True)
    broker = SimulatedBroker()
    engine, _, execution_store = make_engine(
        tmp_path,
        risk_store,
        policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION),
        broker=broker,
    )
    planned = asyncio.run(engine.plan_decision(decision, now_ms=NOW + 2))
    stored = execution_store.load_command(planned.command.command_id)
    assert stored is not None
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    partial = broker.fill(
        primary,
        fill_quantity=primary.quantity / 2,
        fill_price=100.1,
        fee=0.1,
        event_time=NOW + 3,
    )
    stored = asyncio.run(engine.record_order_update(partial))
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    overfill = broker.fill(
        protective,
        fill_quantity=protective.quantity,
        fill_price=99,
        fee=0.1,
        event_time=NOW + 4,
    )

    with pytest.raises(ExecutionUpdateConflict, match="exceeds primary filled exposure"):
        asyncio.run(engine.record_order_update(overfill))
    assert execution_store.load_order_update(overfill.update_id) is None
''',
)

Path("tests/test_execution_network_boundary.py").write_text(
    '''import ast
from pathlib import Path


BANNED_IMPORTS = {"aiohttp", "ccxt", "httpx", "requests", "socket", "websockets"}
BANNED_CALLS = {
    "amend_order",
    "cancel_order",
    "create_order",
    "edit_order",
    "place_order",
    "submit_order",
}


def test_execution_modules_have_no_live_network_or_exchange_write_path() -> None:
    root = Path("src/crypto_quant")
    violations = []
    for path in sorted(root.glob("execution*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in BANNED_IMPORTS:
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".", 1)[0]
                if module in BANNED_IMPORTS:
                    violations.append(f"{path}:{node.lineno}: from {node.module}")
            elif isinstance(node, ast.Call):
                function = node.func
                name = None
                if isinstance(function, ast.Name):
                    name = function.id
                elif isinstance(function, ast.Attribute):
                    name = function.attr
                if name in BANNED_CALLS:
                    violations.append(f"{path}:{node.lineno}: call {name}")
    assert violations == []
''',
    encoding="utf-8",
)
