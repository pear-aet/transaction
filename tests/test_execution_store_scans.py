from crypto_quant.execution_broker import SimulatedBroker
from crypto_quant.execution_contracts import ExecutionMode, ExecutionPolicy
from crypto_quant.execution_store import ExecutionStore
from crypto_quant.strategy_contracts import StrategyAction

from portfolio_fixtures import NOW, execution_command, risk_decision


def test_command_scan_is_ordered_filtered_and_snapshot_bounded(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "execution.db")
    entry = execution_command(risk_decision(storage_sequence=1))
    exit_command = execution_command(
        risk_decision(StrategyAction.EXIT, storage_sequence=2)
    )
    first = store.commit_command(entry, updated_at=NOW).stored_command
    second = store.commit_command(exit_command, updated_at=NOW + 1).stored_command
    assert first.command.storage_sequence == 1
    assert second.command.storage_sequence == 2

    assert [item.command.command_id for item in store.load_commands()] == [
        entry.command_id,
        exit_command.command_id,
    ]
    assert [item.command.command_id for item in store.load_commands(through_sequence=1)] == [
        entry.command_id
    ]
    assert [item.command.command_id for item in store.load_commands(after_sequence=1)] == [
        exit_command.command_id
    ]
    assert [
        item.command.command_id
        for item in store.load_commands(account_id="paper", engine_instance_id="execution-primary")
    ] == [entry.command_id, exit_command.command_id]
    assert store.load_commands(account_id="other") == []


def test_order_update_scan_uses_independent_monotonic_storage_sequence(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "execution.db")
    command = execution_command(risk_decision(storage_sequence=1))
    stored = store.commit_command(command, updated_at=NOW).stored_command
    broker = SimulatedBroker()
    dispatch_updates = broker.dispatch(stored.command, stored.orders, NOW + 1)
    for update in dispatch_updates:
        store.apply_order_update(update)
    store.mark_dispatched(command.command_id, NOW + 1)

    current = store.load_command(command.command_id)
    assert current is not None
    primary = current.orders[0]
    fill = broker.fill(
        primary,
        fill_quantity=primary.quantity,
        fill_price=100.1,
        fee=0.1,
        event_time=NOW + 2,
    )
    store.apply_order_update(fill)

    updates = store.load_order_updates(account_id="paper")
    assert [item.storage_sequence for item in updates] == list(
        range(1, len(updates) + 1)
    )
    assert store.latest_order_update_sequence() == len(updates)
    assert [item.update.update_id for item in store.load_order_updates(through_sequence=1)] == [
        updates[0].update.update_id
    ]
    assert [
        item.update.update_id
        for item in store.load_order_updates(after_sequence=1, command_id=command.command_id)
    ] == [item.update.update_id for item in updates[1:]]
    assert store.load_order_updates(account_id="other") == []


def test_shadow_command_scan_contains_audit_fact_without_order_updates(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "execution.db")
    decision = risk_decision(storage_sequence=1)
    from crypto_quant.execution import build_execution_command
    from crypto_quant.execution_contracts import ExecutionContext, ExecutionMarketSnapshot

    command = build_execution_command(
        ExecutionContext(
            "execution-shadow",
            decision,
            ExecutionMarketSnapshot(
                "shadow-market",
                decision.symbol,
                NOW,
                100,
                bid_price=99.9,
                ask_price=100.1,
            ),
            ExecutionPolicy(mode=ExecutionMode.SHADOW),
            NOW,
        )
    )
    store.commit_command(command, updated_at=NOW)

    rows = store.load_commands(engine_instance_id="execution-shadow")
    assert len(rows) == 1
    assert rows[0].command.mode is ExecutionMode.SHADOW
    assert store.load_order_updates(command_id=command.command_id) == []
