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
ENGINE_TEST = "tests/test_execution_engine.py"
STORE_TEST = "tests/test_execution_store.py"

replace_once(
    STORE,
    '''                    if not primary_order.terminal or primary_order.cumulative_filled_quantity <= 0:
                        raise ValueError("entry completion requires terminal filled exposure")
''',
    '''                    if (
                        primary_order.state
                        not in {
                            OrderState.FILLED,
                            OrderState.CANCELED,
                        }
                        or primary_order.cumulative_filled_quantity <= 0
                    ):
                        raise ValueError(
                            "entry completion requires filled or OCO-canceled exposure"
                        )
''',
)

replace_once(
    ENGINE,
    '''        protective_order = protective[0]
        if protective_order.cumulative_filled_quantity <= 0:
            return stored

        if not primary_order.terminal and primary_order.remaining_quantity > 1e-12:
''',
    '''        protective_order = protective[0]
        protective_terminal_problem = protective_order.state in {
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        }
        if (
            protective_order.cumulative_filled_quantity <= 0
            and not protective_terminal_problem
        ):
            return stored

        if not primary_order.terminal and primary_order.remaining_quantity > 1e-12:
''',
)
replace_once(
    ENGINE,
    '''            if (
                primary_order.cumulative_filled_quantity <= 1e-12
                and primary_order.state in failed_states | {OrderState.CANCELED}
            ):
                failure_reason = f"primary_{primary_order.state.value}_without_fill"
            elif protective_order.state in failed_states:
                failure_reason = f"protective_{protective_order.state.value}"
''',
    '''            if protective_order.state in failed_states:
                failure_reason = f"protective_{protective_order.state.value}"
            elif (
                primary_order.cumulative_filled_quantity <= 1e-12
                and primary_order.state in failed_states | {OrderState.CANCELED}
            ):
                failure_reason = f"primary_{primary_order.state.value}_without_fill"
''',
)
replace_once(
    ENGINE,
    '''                if (
                    commit.order.leg_type is OrderLegType.PROTECTIVE_STOP
                    and commit.order.cumulative_filled_quantity > 0
                ):
                    stored = await self._enforce_entry_oco(stored, update.event_time)
''',
    '''                if (
                    commit.order.leg_type is OrderLegType.PROTECTIVE_STOP
                    and (
                        commit.order.cumulative_filled_quantity > 0
                        or commit.order.state
                        in {
                            OrderState.CANCELED,
                            OrderState.REJECTED,
                            OrderState.EXPIRED,
                            OrderState.FAILED,
                        }
                    )
                ):
                    stored = await self._enforce_entry_oco(stored, update.event_time)
''',
)

replace_once(
    ENGINE_TEST,
    '''    stored = asyncio.run(engine.record_order_update(rejected))
    assert stored.delivery_state is ExecutionDeliveryState.FAILED
    assert stored.failure_reason == "protective_rejected"
''',
    '''    stored = asyncio.run(engine.record_order_update(rejected))
    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    assert stored.delivery_state is ExecutionDeliveryState.FAILED
    assert stored.failure_reason == "protective_rejected"
    assert primary.state is OrderState.CANCELED
    assert primary.cumulative_filled_quantity == pytest.approx(primary.quantity / 2)
''',
)
append_once(
    ENGINE_TEST,
    "test_unexpected_protective_cancel_cancels_primary_remainder_before_failure",
    '''def test_unexpected_protective_cancel_cancels_primary_remainder_before_failure(
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
    stored = asyncio.run(
        engine.record_order_update(
            broker.fill(
                primary,
                fill_quantity=primary.quantity / 2,
                fill_price=100.1,
                fee=0.1,
                event_time=NOW + 3,
            )
        )
    )
    protective = next(
        order for order in stored.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    for update in broker.cancel(
        protective,
        event_time=NOW + 4,
        reason="unexpected_protection_cancel",
    ):
        stored = asyncio.run(engine.record_order_update(update))

    primary = next(order for order in stored.orders if order.leg_type is OrderLegType.PRIMARY)
    assert stored.delivery_state is ExecutionDeliveryState.FAILED
    assert stored.failure_reason == "protective_canceled_with_open_fill"
    assert primary.state is OrderState.CANCELED
''',
)

append_once(
    STORE_TEST,
    "test_failed_primary_cannot_bypass_entry_completion_invariant",
    '''def test_failed_primary_cannot_bypass_entry_completion_invariant(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "quant.db")
    value = command(policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION))
    stored = store.commit_command(value, updated_at=NOW).stored_command
    broker = SimulatedBroker()
    for update in broker.dispatch(stored.command, stored.orders, NOW + 1):
        store.apply_order_update(update)
    store.mark_dispatched(value.command_id, NOW + 1)

    current = store.load_command(value.command_id)
    assert current is not None
    primary = current.orders[0]
    quantity = primary.quantity / 2
    store.apply_order_update(
        broker.fill(
            primary,
            fill_quantity=quantity,
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
    primary = current.orders[0]
    protective = current.orders[1]
    store.apply_order_update(
        broker.fill(
            protective,
            fill_quantity=quantity,
            fill_price=99,
            fee=0.1,
            event_time=NOW + 3,
        )
    )
    store.apply_order_update(
        OrderUpdate(
            "primary-failed-after-partial",
            primary.order_id,
            NOW + 4,
            OrderState.FAILED,
            quantity,
            quantity * 100.1,
            0.1,
            average_fill_price=100.1,
            broker_order_id=primary.broker_order_id,
            evidence={"source": "failure_test"},
        )
    )
    protective = store.load_order(protective.order_id)
    assert protective is not None
    for update in broker.cancel(
        protective,
        event_time=NOW + 4,
        reason="flat_after_primary_failure",
    ):
        store.apply_order_update(update)

    with pytest.raises(ValueError, match="filled or OCO-canceled"):
        store.mark_completed(value.command_id, NOW + 5)
''',
)
