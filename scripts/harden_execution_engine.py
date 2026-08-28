from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/crypto_quant/execution_engine.py",
    "import asyncio\nimport time\n",
    "import asyncio\nimport math\nimport time\n",
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''    def _load_source(self, command: ExecutionCommand) -> StoredRiskDecision:
''',
    '''    def _load_input_source(self, decision: RiskDecision) -> StoredRiskDecision:
        sequence = int(decision.storage_sequence or 0)
        rows = self.risk_store.load_decisions(
            after_sequence=max(0, sequence - 1),
            through_sequence=sequence,
            limit=1,
        )
        if len(rows) != 1:
            raise ExecutionSourceError(
                f"risk decision storage sequence is missing: {sequence}"
            )
        stored = rows[0]
        if stored.decision.storage_sequence != sequence:
            raise ExecutionSourceError("risk decision storage sequence mismatch")
        if stored.decision.semantic_dict() != decision.semantic_dict():
            raise ExecutionSourceError(
                f"risk decision semantics differ from RiskStore: {decision.decision_id}"
            )
        return stored

    def _load_source(self, command: ExecutionCommand) -> StoredRiskDecision:
''',
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''        if decision.approved_action is not command.action:
            mismatches.append("approved_action")
        if mismatches:
''',
    '''        if decision.approved_action is not command.action:
            mismatches.append("approved_action")
        for name, actual, expected in (
            ("quantity", command.quantity, decision.quantity),
            ("reference_price", command.reference_price, decision.reference_price),
            ("leverage", command.leverage, decision.leverage),
            ("required_margin", command.required_margin, decision.required_margin),
        ):
            if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12):
                mismatches.append(name)
        if command.stop_price != decision.stop_price:
            mismatches.append("stop_price")
        if command.reduce_only != decision.reduce_only:
            mismatches.append("reduce_only")
        if command.valid_until > decision.valid_until:
            mismatches.append("valid_until")
        if command.evidence.get("source_risk_valid_until") != decision.valid_until:
            mismatches.append("source_risk_valid_until")
        if mismatches:
''',
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''            completed = (
                primary_order.state is OrderState.FILLED
                and protective_order.state in {OrderState.FILLED, OrderState.CANCELED}
            )
''',
    '''            completed = (
                primary_order.state is OrderState.FILLED
                and protective_order.state is OrderState.FILLED
            )
''',
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''    async def flush_outbox(self, *, now_ms: int | None = None) -> ExecutionBootstrapResult:
''',
    '''    async def _flush_outbox_unlocked(
        self,
        *,
        now_ms: int | None = None,
    ) -> ExecutionBootstrapResult:
''',
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''    async def bootstrap(self, *, now_ms: int | None = None) -> ExecutionBootstrapResult:
''',
    '''    async def flush_outbox(
        self,
        *,
        now_ms: int | None = None,
    ) -> ExecutionBootstrapResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        async with self._processing_lock:
            try:
                result = await self._flush_outbox_unlocked(now_ms=current)
                self.health.last_error = None
                return result
            except Exception as exc:
                self._record_failure(exc)
                raise

    async def bootstrap(self, *, now_ms: int | None = None) -> ExecutionBootstrapResult:
''',
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    "                result = await self.flush_outbox(now_ms=current)\n",
    "                result = await self._flush_outbox_unlocked(now_ms=current)\n",
)
text_path = Path("src/crypto_quant/execution_engine.py")
text = text_path.read_text(encoding="utf-8")
old = "                        await self.flush_outbox(now_ms=current)\n"
count = text.count(old)
if count != 2:
    raise SystemExit(f"expected two internal outbox calls, found {count}")
text_path.write_text(
    text.replace(old, "                        await self._flush_outbox_unlocked(now_ms=current)\n"),
    encoding="utf-8",
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''            try:
                existing = await asyncio.to_thread(
                    self.store.load_by_source_decision,
''',
    '''            try:
                await asyncio.to_thread(self._load_input_source, decision)
                existing = await asyncio.to_thread(
                    self.store.load_by_source_decision,
''',
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''            try:
                commit = await asyncio.to_thread(self.store.apply_order_update, update)
''',
    '''            try:
                existing_update = await asyncio.to_thread(
                    self.store.load_order_update,
                    update.update_id,
                )
                if existing_update is None:
                    current_order = await asyncio.to_thread(
                        self.store.load_order,
                        update.order_id,
                    )
                    if current_order is None:
                        raise KeyError(f"execution order not found: {update.order_id}")
                    current_command = await asyncio.to_thread(
                        self.store.load_command,
                        current_order.command_id,
                    )
                    if current_command is None:
                        raise RuntimeError("execution order has no persisted command")
                    if current_command.command.engine_instance_id != self.settings.engine_instance_id:
                        raise ExecutionSourceError("order belongs to another execution instance")
                    if current_command.command.mode is not ExecutionMode.SIMULATION:
                        raise ExecutionDispatchError("shadow command cannot receive order updates")
                    if current_command.delivery_state is not ExecutionDeliveryState.DISPATCHED:
                        raise ExecutionDispatchError(
                            "new order updates require a dispatched simulation command"
                        )
                commit = await asyncio.to_thread(self.store.apply_order_update, update)
''',
)

replace_once(
    "src/crypto_quant/execution_store.py",
    '''    def order_updates(self, order_id: str) -> list[StoredOrderUpdate]:
''',
    '''    def load_order_update(self, update_id: str) -> StoredOrderUpdate | None:
        if not update_id:
            raise ValueError("update_id must not be empty")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_order_updates WHERE update_id=?",
                (update_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredOrderUpdate(
            update=self._decode_update(str(row["payload"])),
            storage_sequence=int(row["storage_sequence"]),
            prior_state=OrderState(row["prior_state"]),
            new_state=OrderState(row["new_state"]),
        )

    def order_updates(self, order_id: str) -> list[StoredOrderUpdate]:
''',
)
