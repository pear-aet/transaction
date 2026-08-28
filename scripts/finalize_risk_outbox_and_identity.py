from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/crypto_quant/risk_store.py",
    '''    def pending_decisions(
        self,
        *,
        engine_instance_id: str | None = None,
        limit: int = 1000,
    ) -> list[RiskDecision]:
        if not 1 <= limit <= 10_000:
            raise ValueError("decision limit must be in [1, 10000]")
        query = """SELECT * FROM risk_decisions
        WHERE delivery_state='pending'
        AND (? IS NULL OR engine_instance_id=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (engine_instance_id, engine_instance_id, limit)
''',
    '''    def pending_decisions(
        self,
        *,
        engine_instance_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[RiskDecision]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("decision limit must be in [1, 10000]")
        query = """SELECT * FROM risk_decisions
        WHERE delivery_state='pending'
        AND storage_sequence>?
        AND (? IS NULL OR engine_instance_id=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (after_sequence, engine_instance_id, engine_instance_id, limit)
''',
)

replace_once(
    "src/crypto_quant/risk_engine.py",
    '''    async def _flush_outbox(self, now_ms: int) -> tuple[int, int]:
        published = 0
        expired = 0
        while True:
            pending = await asyncio.to_thread(
                self.store.pending_decisions,
                engine_instance_id=self.settings.engine_instance_id,
                limit=self.settings.outbox_batch_size,
            )
            if not pending:
                return published, expired
            for decision in pending:
                if now_ms > decision.valid_until:
                    await asyncio.to_thread(
                        self.store.mark_expired,
                        decision.decision_id,
                        now_ms,
                    )
                    self.health.decisions_expired += 1
                    expired += 1
                    continue
                await self._assert_execution_subscriber()
''',
    '''    async def _expire_stale_outbox(self, now_ms: int) -> int:
        cursor = 0
        expired = 0
        while True:
            pending = await asyncio.to_thread(
                self.store.pending_decisions,
                engine_instance_id=self.settings.engine_instance_id,
                after_sequence=cursor,
                limit=self.settings.outbox_batch_size,
            )
            if not pending:
                return expired
            for decision in pending:
                if decision.storage_sequence is None:
                    raise RuntimeError("pending risk decision has no storage sequence")
                cursor = decision.storage_sequence
                if now_ms > decision.valid_until:
                    changed = await asyncio.to_thread(
                        self.store.mark_expired,
                        decision.decision_id,
                        now_ms,
                    )
                    if changed:
                        self.health.decisions_expired += 1
                        expired += 1
            if len(pending) < self.settings.outbox_batch_size:
                return expired

    async def _flush_outbox(self, now_ms: int) -> tuple[int, int]:
        expired = await self._expire_stale_outbox(now_ms)
        published = 0
        while True:
            pending = await asyncio.to_thread(
                self.store.pending_decisions,
                engine_instance_id=self.settings.engine_instance_id,
                limit=self.settings.outbox_batch_size,
            )
            if not pending:
                return published, expired
            for decision in pending:
                await self._assert_execution_subscriber()
''',
)

replace_once(
    "src/crypto_quant/risk.py",
    '''    if not math.isclose(
        decision.reference_price,
        context.intent.reference_price,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        mismatches.append("reference_price")
    if mismatches:
''',
    '''    if not math.isclose(
        decision.reference_price,
        context.intent.reference_price,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        mismatches.append("reference_price")
    if decision.status is RiskDecisionStatus.REJECTED:
        expected_valid_until = context.evaluated_at
    elif decision.requested_action is StrategyAction.EXIT:
        expected_valid_until = (
            context.intent.decision_time + context.policy.max_intent_age_ms
        )
    else:
        expected_valid_until = min(
            context.intent.decision_time + context.policy.max_intent_age_ms,
            context.account.observed_at + context.policy.max_account_age_ms,
            context.market.observed_at + context.policy.max_market_age_ms,
        )
    if decision.valid_until != expected_valid_until:
        mismatches.append("valid_until")
    expected_decision_id = _decision_id(
        context,
        status=decision.status,
        approved_action=decision.approved_action,
        reasons=decision.reasons,
        quantity=decision.quantity,
        notional=decision.notional,
        leverage=decision.leverage,
        stop_price=decision.stop_price,
        valid_until=decision.valid_until,
    )
    if decision.decision_id != expected_decision_id:
        mismatches.append("decision_id")
    if mismatches:
''',
)

replace_once(
    "src/crypto_quant/engine.py",
    "                    correlation_group=key,\n",
    '                    correlation_group="default",\n',
)
replace_once(
    "src/crypto_quant/engine.py",
    "            correlation_group=symbol,\n",
    '            correlation_group="default",\n',
)
