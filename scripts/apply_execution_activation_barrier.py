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
STORE_TEST = "tests/test_execution_store.py"

replace_once(
    STORE,
    '''            if command_state is ExecutionDeliveryState.PENDING:
                if (
                    update.new_state
                    not in {
                        OrderState.DISPATCHED,
                        OrderState.ACKNOWLEDGED,
                    }
                    or update.cumulative_filled_quantity != 0
                ):
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
                if update.cumulative_filled_quantity > primary.cumulative_filled_quantity + 1e-12:
                    raise ExecutionUpdateConflict("protective fill exceeds primary filled exposure")
''',
    '''            if command_state is ExecutionDeliveryState.PENDING:
                if current.leg_type is not OrderLegType.PRIMARY:
                    raise ExecutionUpdateConflict(
                        "pending command cannot update an activation-bound protective order"
                    )
                if (
                    update.new_state
                    not in {
                        OrderState.DISPATCHED,
                        OrderState.ACKNOWLEDGED,
                    }
                    or update.cumulative_filled_quantity != 0
                ):
                    raise ExecutionUpdateConflict(
                        "pending command accepts primary dispatch and acknowledgement only"
                    )
            if current.leg_type is OrderLegType.PROTECTIVE_STOP:
                primary_row = connection.execute(
                    """SELECT payload FROM execution_orders
                    WHERE command_id=? AND leg_type='primary'""",
                    (current.command_id,),
                ).fetchone()
                if primary_row is None:
                    raise RuntimeError("protective order has no primary order")
                primary = self._decode_order(str(primary_row["payload"]))
                if (
                    current.state is OrderState.PLANNED
                    and update.new_state is OrderState.DISPATCHED
                    and primary.cumulative_filled_quantity <= 1e-12
                ):
                    raise ExecutionUpdateConflict(
                        "protective order activation requires primary filled exposure"
                    )
                if (
                    update.cumulative_filled_quantity
                    > primary.cumulative_filled_quantity + 1e-12
                ):
                    raise ExecutionUpdateConflict(
                        "protective fill exceeds primary filled exposure"
                    )
            elif (
                current.leg_type is OrderLegType.PRIMARY
                and update.cumulative_filled_quantity
                > current.cumulative_filled_quantity + 1e-12
            ):
                protective_row = connection.execute(
                    """SELECT payload FROM execution_orders
                    WHERE command_id=? AND leg_type='protective_stop'""",
                    (current.command_id,),
                ).fetchone()
                if protective_row is not None:
                    protective = self._decode_order(str(protective_row["payload"]))
                    protection_has_triggered = (
                        protective.cumulative_filled_quantity > 1e-12
                        or protective.state
                        in {
                            OrderState.CANCEL_PENDING,
                            OrderState.CANCELED,
                            OrderState.REJECTED,
                            OrderState.EXPIRED,
                            OrderState.FAILED,
                            OrderState.FILLED,
                        }
                    )
                    if protection_has_triggered:
                        raise ExecutionUpdateConflict(
                            "primary fill cannot increase after protective execution starts"
                        )
''',
)

replace_once(
    ENGINE,
    '''        protective_terminal_problem = protective_order.state in {
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        }
''',
    '''        protective_terminal_problem = protective_order.state in {
            OrderState.CANCEL_PENDING,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        }
''',
)
replace_once(
    ENGINE,
    '''                    in {
                        OrderState.CANCELED,
                        OrderState.REJECTED,
                        OrderState.EXPIRED,
                        OrderState.FAILED,
                    }
''',
    '''                    in {
                        OrderState.CANCEL_PENDING,
                        OrderState.CANCELED,
                        OrderState.REJECTED,
                        OrderState.EXPIRED,
                        OrderState.FAILED,
                    }
''',
)

append_once(
    STORE_TEST,
    "test_protective_activation_is_blocked_until_primary_exposure_exists",
    '''def test_protective_activation_is_blocked_until_primary_exposure_exists(
    tmp_path,
) -> None:
    store = ExecutionStore(tmp_path / "quant.db")
    value = command(policy=ExecutionPolicy(mode=ExecutionMode.SIMULATION))
    stored = store.commit_command(value, updated_at=NOW).stored_command
    broker = SimulatedBroker()
    protective = stored.orders[1]
    premature = OrderUpdate(
        "premature-protective-dispatch",
        protective.order_id,
        NOW + 1,
        OrderState.DISPATCHED,
        0,
        0,
        0,
        broker_order_id="sim-premature-protection",
        evidence={"source": "activation_test"},
    )

    with pytest.raises(ExecutionUpdateConflict, match="pending command cannot update"):
        store.apply_order_update(premature)

    for update in broker.dispatch(stored.command, stored.orders, NOW + 1):
        store.apply_order_update(update)
    store.mark_dispatched(value.command_id, NOW + 1)
    with pytest.raises(ExecutionUpdateConflict, match="requires primary filled exposure"):
        store.apply_order_update(replace(premature, update_id="premature-after-dispatch"))


def test_primary_fill_cannot_increase_after_protective_execution_starts(tmp_path) -> None:
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
    first_fill = primary.quantity / 2
    store.apply_order_update(
        broker.fill(
            primary,
            fill_quantity=first_fill,
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
            fill_quantity=first_fill / 2,
            fill_price=99,
            fee=0.05,
            event_time=NOW + 3,
        )
    )

    with pytest.raises(ExecutionUpdateConflict, match="cannot increase after protective"):
        store.apply_order_update(
            broker.fill(
                primary,
                fill_quantity=first_fill / 2,
                fill_price=100.2,
                fee=0.05,
                event_time=NOW + 4,
            )
        )
''',
)
