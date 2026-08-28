from pathlib import Path

path = Path("tests/test_execution_recovery.py")
text = path.read_text(encoding="utf-8")
old = '''        execution_store.commit_command(command, updated_at=NOW)
        execution_store.mark_dispatched(command.command_id, NOW)
        assert [item.command.command_id for item in execution_store.active_commands()] == [
'''
new = '''        stored = execution_store.commit_command(command, updated_at=NOW).stored_command
        broker = SimulatedBroker()
        for update in broker.dispatch(stored.command, stored.orders, NOW):
            execution_store.apply_order_update(update)
        execution_store.mark_dispatched(command.command_id, NOW)
        assert [item.command.command_id for item in execution_store.active_commands()] == [
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one recovery setup match, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
