from pathlib import Path

path = Path("tests/test_execution_runtime.py")
text = path.read_text(encoding="utf-8")
old = '''        await wait_until(
            lambda: execution_store.metrics()["commands"] == 2,
            "periodic durable catch-up did not release the upstream barrier",
        )
'''
new = '''        await wait_until(
            lambda: (
                execution_store.metrics()["commands"] == 2
                and runtime.replay_ledger.offset(
                    "execution-upstream-v1",
                    runtime._durable_filter(),
                ).sequence
                == second.storage_sequence
            ),
            "periodic durable catch-up did not commit commands and cursor",
        )
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one cursor wait match, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
