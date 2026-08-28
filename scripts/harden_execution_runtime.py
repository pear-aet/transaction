from pathlib import Path


path = Path("src/crypto_quant/execution_runtime.py")
text = path.read_text(encoding="utf-8")
old = '''    async def _periodic_flush(self) -> None:
        await self.execution_engine.flush_outbox(now_ms=self.clock())
        self.health.periodic_flushes += 1
'''
new = '''    async def _periodic_flush(self) -> None:
        await self.execution_engine.flush_outbox(now_ms=self.clock())
        await self._catch_up()
        self.health.periodic_flushes += 1
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected exactly one periodic flush block, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
