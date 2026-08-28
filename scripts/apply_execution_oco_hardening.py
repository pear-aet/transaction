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
BROKER = "src/crypto_quant/execution_broker.py"
ENGINE = "src/crypto_quant/execution_engine.py"
ENGINE_TEST = "tests/test_execution_engine.py"
RECOVERY_TEST = "tests/test_execution_recovery.py"

replace_once(
    STORE,
    "import json\nimport sqlite3\n",
    "import json\nimport math\nimport sqlite3\n",
)
replace_once(
    STORE,
    '''            elif target is ExecutionDeliveryState.COMPLETED:
                if len(primary) != 1 or primary[0].state is not OrderState.FILLED:
                    raise ValueError("completion requires a filled primary order")
                if protective and any(order.state is not OrderState.FILLED for order in protective):
                    raise ValueError("entry completion requires filled protective closure")
''',
    '''            elif target is ExecutionDeliveryState.COMPLETED:
                if len(primary) != 1:
                    raise ValueError("completion requires one primary order")
                primary_order = primary[0]
                if not protective:
                    if primary_order.state is not OrderState.FILLED:
                        raise ValueError("exit completion requires a filled primary order")
                else:
                    if len(protective) != 1:
                        raise ValueError("entry completion requires one protective order")
                    protective_order = protective[0]
                    if not primary_order.terminal or primary_order.cumulative_filled_quantity <= 0:
                        raise ValueError("entry completion requires terminal filled exposure")
                    if protective_order.state not in {
                        OrderState.FILLED,
                        OrderState.CANCELED,
                    }:
                        raise ValueError("entry completion requires terminal protective closure")
                    if not math.isclose(
                        primary_order.cumulative_filled_quantity,
                        protective_order.cumulative_filled_quantity,
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    ):
                        raise ValueError("entry completion requires zero remaining exposure")
''',
)

replace_once(
    BROKER,
    '''    def activate_protection(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...],
        now_ms: int,
    ) -> tuple[OrderUpdate, ...]: ...


class ShadowBroker:
''',
    '''    def activate_protection(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...],
        now_ms: int,
    ) -> tuple[OrderUpdate, ...]: ...

    def cancel(
        self,
        order: ExecutionOrder,
        *,
        event_time: int,
        reason: str,
    ) -> tuple[OrderUpdate, ...]: ...


class ShadowBroker:
''',
)
replace_once(
    BROKER,
    '''    def activate_protection(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...],
        now_ms: int,
    ) -> tuple[OrderUpdate, ...]:
        del command, orders, now_ms
        raise ExecutionBrokerError("shadow execution has no protection dispatch capability")


class SimulatedBroker:
''',
    '''    def activate_protection(
        self,
        command: ExecutionCommand,
        orders: tuple[ExecutionOrder, ...],
        now_ms: int,
    ) -> tuple[OrderUpdate, ...]:
        del command, orders, now_ms
        raise ExecutionBrokerError("shadow execution has no protection dispatch capability")

    def cancel(
        self,
        order: ExecutionOrder,
        *,
        event_time: int,
        reason: str,
    ) -> tuple[OrderUpdate, ...]:
        del order, event_time, reason
        raise ExecutionBrokerError("shadow execution has no cancellation capability")


class SimulatedBroker:
''',
)

replace_once(
    ENGINE,
    '''    async def _apply_updates(self, updates: tuple[OrderUpdate, ...]) -> int:
        applied = 0
        for update in updates:
            commit = await asyncio.to_thread(self.store.apply_order_update, update)
            if not commit.duplicate:
                applied += 1
                self.health.order_updates += 1
        return applied

    async def _reconcile_command(
''',
    '''    async def _apply_updates(self, updates: tuple[OrderUpdate, ...]) -> int:
        applied = 0
        for update in updates:
            commit = await asyncio.to_thread(self.store.apply_order_update, update)
            if not commit.duplicate:
                applied += 1
                self.health.order_updates += 1
        return applied

    async def _enforce_entry_oco(
        self,
        stored: StoredExecutionCommand,
        now_ms: int,
    ) -> StoredExecutionCommand:
        if self.policy.mode is not ExecutionMode.SIMULATION or self.broker is None:
            return stored
        primary = [order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY]
        protective = [
            order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
        ]
        if not protective:
            return stored
        if len(primary) != 1 or len(protective) != 1:
            raise RuntimeError("entry command has invalid OCO order topology")
        primary_order = primary[0]
        protective_order = protective[0]
        if protective_order.cumulative_filled_quantity <= 0:
            return stored

        if not primary_order.terminal and primary_order.remaining_quantity > 1e-12:
            cancellation = await asyncio.to_thread(
                self.broker.cancel,
                primary_order,
                event_time=now_ms,
                reason="protective_fill_cancel_primary_remainder",
            )
            await self._apply_updates(cancellation)
            refreshed = await asyncio.to_thread(
                self.store.load_command,
                stored.command.command_id,
            )
            if refreshed is None:
                raise RuntimeError("entry command disappeared after primary OCO cancellation")
            stored = refreshed
            primary_order = next(
                order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY
            )
            protective_order = next(
                order
                for order in stored.orders
                if order.leg_type is OrderLegType.PROTECTIVE_STOP
            )

        open_quantity = (
            primary_order.cumulative_filled_quantity
            - protective_order.cumulative_filled_quantity
        )
        if open_quantity < -1e-12:
            raise RuntimeError("protective execution exceeded primary exposure")
        if (
            math.isclose(open_quantity, 0.0, rel_tol=1e-12, abs_tol=1e-12)
            and not protective_order.terminal
            and protective_order.remaining_quantity > 1e-12
        ):
            cancellation = await asyncio.to_thread(
                self.broker.cancel,
                protective_order,
                event_time=now_ms,
                reason="flat_position_cancel_protective_remainder",
            )
            await self._apply_updates(cancellation)
            refreshed = await asyncio.to_thread(
                self.store.load_command,
                stored.command.command_id,
            )
            if refreshed is None:
                raise RuntimeError("entry command disappeared after protective OCO cancellation")
            stored = refreshed
        return stored

    async def _reconcile_command(
''',
)
replace_once(
    ENGINE,
    '''        failed_states = {
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        }
        failure_reason: str | None = None
        if primary_order.state in failed_states:
            failure_reason = f"primary_{primary_order.state.value}"
        elif primary_order.state is OrderState.CANCELED:
            if primary_order.cumulative_filled_quantity <= 0:
                failure_reason = "primary_canceled_without_fill"
        if protective:
            protective_order = protective[0]
            if protective_order.state in failed_states:
                failure_reason = f"protective_{protective_order.state.value}"
            elif protective_order.state is OrderState.CANCELED:
                if primary_order.cumulative_filled_quantity > 0:
                    failure_reason = "protective_canceled_with_open_fill"

        if failure_reason is not None:
            if stored.delivery_state is ExecutionDeliveryState.DISPATCHED:
                changed = await asyncio.to_thread(
                    self.store.mark_failed,
                    stored.command.command_id,
                    now_ms,
                    failure_reason,
                    evidence={"source": "order_reconciliation"},
                )
                if changed:
                    self.health.commands_failed += 1
            return

        completed = False
        if not protective:
            completed = primary_order.state is OrderState.FILLED
        else:
            protective_order = protective[0]
            completed = (
                primary_order.state is OrderState.FILLED
                and protective_order.state is OrderState.FILLED
            )
''',
    '''        failed_states = {
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        }
        failure_reason: str | None = None
        completed = False
        if not protective:
            if primary_order.state is OrderState.FILLED:
                completed = True
            elif primary_order.state in failed_states | {OrderState.CANCELED}:
                failure_reason = f"primary_{primary_order.state.value}_incomplete"
        else:
            if len(protective) != 1:
                raise RuntimeError("persisted entry command has invalid protection topology")
            protective_order = protective[0]
            open_quantity = (
                primary_order.cumulative_filled_quantity
                - protective_order.cumulative_filled_quantity
            )
            if open_quantity < -1e-12:
                raise RuntimeError("persisted protection exceeds primary exposure")
            open_quantity = max(0.0, open_quantity)
            if (
                primary_order.cumulative_filled_quantity <= 1e-12
                and primary_order.state in failed_states | {OrderState.CANCELED}
            ):
                failure_reason = f"primary_{primary_order.state.value}_without_fill"
            elif protective_order.state in failed_states:
                failure_reason = f"protective_{protective_order.state.value}"
            elif (
                protective_order.state is OrderState.CANCELED
                and open_quantity > 1e-12
            ):
                failure_reason = "protective_canceled_with_open_fill"
            elif (
                primary_order.state in failed_states
                and primary_order.cumulative_filled_quantity > 1e-12
                and math.isclose(open_quantity, 0.0, rel_tol=1e-12, abs_tol=1e-12)
            ):
                failure_reason = f"primary_{primary_order.state.value}_partial_fill_closed"
            else:
                completed = (
                    primary_order.terminal
                    and primary_order.cumulative_filled_quantity > 1e-12
                    and math.isclose(
                        open_quantity,
                        0.0,
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    )
                    and protective_order.state
                    in {
                        OrderState.FILLED,
                        OrderState.CANCELED,
                    }
                )

        if failure_reason is not None:
            if stored.delivery_state is ExecutionDeliveryState.DISPATCHED:
                changed = await asyncio.to_thread(
                    self.store.mark_failed,
                    stored.command.command_id,
                    now_ms,
                    failure_reason,
                    evidence={"source": "order_reconciliation"},
                )
                if changed:
                    self.health.commands_failed += 1
            return
''',
)
replace_once(
    ENGINE,
    '''            for stored in active:
                await self._reconcile_command(stored, now_ms)
                reconciled += 1
''',
    '''            for stored in active:
                stored = await self._enforce_entry_oco(stored, now_ms)
                await self._reconcile_command(stored, now_ms)
                reconciled += 1
''',
)
replace_once(
    ENGINE,
    '''                        if stored is None:
                            raise RuntimeError(
                                "execution command disappeared after protection activation"
                            )
                await self._reconcile_command(stored, update.event_time)
''',
    '''                        if stored is None:
                            raise RuntimeError(
                                "execution command disappeared after protection activation"
                            )
                if (
                    commit.order.leg_type is OrderLegType.PROTECTIVE_STOP
                    and commit.order.cumulative_filled_quantity > 0
                ):
                    stored = await self._enforce_entry_oco(stored, update.event_time)
                await self._reconcile_command(stored, update.event_time)
''',
)

replace_once(
    ENGINE_TEST,
    '''    MarketRiskSnapshot,
    RiskContext,
''',
    '''    MarketRiskSnapshot,
    PositionRiskSnapshot,
    RiskContext,
''',
)
replace_once(
    ENGINE_TEST,
    '''    return stored


class MarketProvider:
''',
    '''    return stored


def persist_exit_risk_decision(store: RiskStore, *, published: bool):
    intent = StrategyIntent(
        intent_id="intent-exit",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.EXIT,
        decision_time=DECISION_TIME,
        created_at=DECISION_TIME,
        reference_price=100,
        stop_price=None,
        reason="execution_exit_test",
        source_event_id="market-exit",
        source_sequence=99,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=99,
    )
    position = PositionRiskSnapshot(
        SYMBOL,
        "long",
        quantity=2,
        entry_price=100,
        mark_price=100,
        initial_risk=50,
        maintenance_margin_rate=0.005,
        stop_price=90,
    )
    account = AccountRiskSnapshot(
        "account-exit",
        "paper",
        DECISION_TIME,
        10_000,
        9_000,
        position.notional * position.maintenance_margin_rate,
        10_000,
        10_000,
        0,
        (position,),
    )
    market = MarketRiskSnapshot(
        "risk-market-exit",
        SYMBOL,
        DECISION_TIME,
        100,
        rules(),
    )
    decision = evaluate_risk(
        RiskContext(
            "risk-primary",
            intent,
            account,
            market,
            RiskPolicy(),
            NOW,
        )
    )
    assert decision.status is RiskDecisionStatus.APPROVED
    stored = store.commit_evaluation(decision, updated_at=NOW).stored_decision.decision
    if published:
        store.mark_published(stored.decision_id, NOW)
    return stored


class MarketProvider:
''',
)
append_once(
    ENGINE_TEST,
    "test_protective_fill_cancels_both_remainders_and_completes_flat_entry",
    '''def test_protective_fill_cancels_both_remainders_and_completes_flat_entry(
    tmp_path,
) -> None:
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
    primary_quantity = primary.quantity / 2
    stored = asyncio.run(
        engine.record_order_update(
            broker.fill(
                primary,
                fill_quantity=primary_quantity,
                fill_price=100.1,
                fee=0.1,
                event_time=NOW + 3,
            )
        )
    )
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    stored = asyncio.run(
        engine.record_order_update(
            broker.fill(
                protective,
                fill_quantity=primary_quantity,
                fill_price=99,
                fee=0.1,
                event_time=NOW + 4,
            )
        )
    )

    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    assert stored.delivery_state is ExecutionDeliveryState.COMPLETED
    assert primary.state is OrderState.CANCELED
    assert protective.state is OrderState.CANCELED
    assert primary.cumulative_filled_quantity == pytest.approx(primary_quantity)
    assert protective.cumulative_filled_quantity == pytest.approx(primary_quantity)
    assert execution_store.command_transitions(stored.command.command_id)[-1].new_state is (
        ExecutionDeliveryState.COMPLETED
    )


def test_partial_protection_cancels_primary_remainder_without_closing_command(
    tmp_path,
) -> None:
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
    primary_quantity = primary.quantity / 2
    stored = asyncio.run(
        engine.record_order_update(
            broker.fill(
                primary,
                fill_quantity=primary_quantity,
                fill_price=100.1,
                fee=0.1,
                event_time=NOW + 3,
            )
        )
    )
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    first_protection = primary_quantity / 2
    stored = asyncio.run(
        engine.record_order_update(
            broker.fill(
                protective,
                fill_quantity=first_protection,
                fill_price=99,
                fee=0.05,
                event_time=NOW + 4,
            )
        )
    )
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    assert stored.delivery_state is ExecutionDeliveryState.DISPATCHED
    assert primary.state is OrderState.CANCELED
    assert protective.state is OrderState.PARTIALLY_FILLED

    stored = asyncio.run(
        engine.record_order_update(
            broker.fill(
                protective,
                fill_quantity=primary_quantity - first_protection,
                fill_price=99,
                fee=0.05,
                event_time=NOW + 5,
            )
        )
    )
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    assert stored.delivery_state is ExecutionDeliveryState.COMPLETED
    assert protective.state is OrderState.CANCELED


def test_partial_exit_cancellation_fails_instead_of_remaining_dispatched(tmp_path) -> None:
    risk_store = RiskStore(tmp_path / "risk.db")
    decision = persist_exit_risk_decision(risk_store, published=True)
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
    stored = asyncio.run(
        engine.record_order_update(
            broker.fill(
                primary,
                fill_quantity=primary.quantity / 2,
                fill_price=99.9,
                fee=0.1,
                event_time=NOW + 3,
            )
        )
    )
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    for update in broker.cancel(
        primary,
        event_time=NOW + 4,
        reason="partial_exit_cancel",
    ):
        stored = asyncio.run(engine.record_order_update(update))

    assert stored.delivery_state is ExecutionDeliveryState.FAILED
    assert stored.failure_reason == "primary_canceled_incomplete"
''',
)

append_once(
    RECOVERY_TEST,
    "test_startup_reconciliation_finishes_oco_after_protective_fill_crash",
    '''def test_startup_reconciliation_finishes_oco_after_protective_fill_crash(
    tmp_path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "quant.db"
        risk_store = RiskStore(database)
        decision = persisted_decision(risk_store)
        execution_store = ExecutionStore(database)
        broker = SimulatedBroker()
        engine = ExecutionEngine(
            ExecutionEngineSettings("execution-primary", "paper"),
            ExecutionPolicy(mode=ExecutionMode.SIMULATION),
            execution_market,
            store=execution_store,
            risk_store=risk_store,
            broker=broker,
        )
        planned = await engine.plan_decision(decision, now_ms=NOW)
        stored = execution_store.load_command(planned.command.command_id)
        assert stored is not None
        primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
        primary_quantity = primary.quantity / 2
        stored = await engine.record_order_update(
            broker.fill(
                primary,
                fill_quantity=primary_quantity,
                fill_price=100.1,
                fee=0.1,
                event_time=NOW + 1,
            )
        )
        protective = next(
            order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
        )
        execution_store.apply_order_update(
            broker.fill(
                protective,
                fill_quantity=primary_quantity,
                fill_price=99,
                fee=0.1,
                event_time=NOW + 2,
            )
        )

        restarted = ExecutionEngine(
            ExecutionEngineSettings("execution-primary", "paper"),
            ExecutionPolicy(mode=ExecutionMode.SIMULATION),
            execution_market,
            store=execution_store,
            risk_store=risk_store,
            broker=broker,
        )
        result = await restarted.bootstrap(now_ms=NOW + 3)
        recovered = execution_store.load_command(planned.command.command_id)

        assert result.reconciled == 1
        assert recovered is not None
        assert recovered.delivery_state is ExecutionDeliveryState.COMPLETED
        primary = next(
            order for order in recovered.orders if order.leg_type is OrderLegType.PRIMARY
        )
        protective = next(
            order
            for order in recovered.orders
            if order.leg_type is OrderLegType.PROTECTIVE_STOP
        )
        assert primary.state is OrderState.CANCELED
        assert protective.state is OrderState.CANCELED

    asyncio.run(scenario())
''',
)
