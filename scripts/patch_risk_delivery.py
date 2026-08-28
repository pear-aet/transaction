from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/crypto_quant/risk_bus.py",
    '''    async def publish(self, decision: RiskDecision) -> RiskPublishResult:
        async with self._publish_lock:
''',
    '''    async def publish(
        self,
        decision: RiskDecision,
        *,
        now_ms: int | None = None,
    ) -> RiskPublishResult:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if current < 0:
            raise ValueError("now_ms must be non-negative")
        async with self._publish_lock:
''',
)
replace_once(
    "src/crypto_quant/risk_bus.py",
    '''            if decision.storage_sequence is None:
                raise ValueError("risk decision must be persisted before publication")
            if decision.sequence != 0:
''',
    '''            if decision.storage_sequence is None:
                raise ValueError("risk decision must be persisted before publication")
            if current > decision.valid_until:
                raise ValueError("expired risk decision cannot be published")
            if decision.sequence != 0:
''',
)
replace_once(
    "src/crypto_quant/risk_bus.py",
    "            self._last_publish_ms = int(time.time() * 1000)\n",
    "            self._last_publish_ms = current\n",
)

replace_once(
    "src/crypto_quant/risk_store.py",
    '''            else:
                instance = self._instance_from_row(row)
                self._assert_instance(instance, decision)

            evaluation_row = connection.execute(
''',
    '''            else:
                instance = self._instance_from_row(row)
                self._assert_instance(instance, decision)
                connection.execute(
                    "UPDATE risk_instances SET updated_at=? WHERE engine_instance_id=?",
                    (updated_at, decision.engine_instance_id),
                )
                instance = RiskInstance(
                    instance.engine_instance_id,
                    instance.account_id,
                    instance.policy_id,
                    instance.policy_version,
                    instance.policy_hash,
                    updated_at,
                )

            evaluation_row = connection.execute(
''',
)
replace_once(
    "src/crypto_quant/risk_store.py",
    '''                if stored.delivery_state not in {
                    expected_state,
                    RiskDeliveryState.PUBLISHED,
                    RiskDeliveryState.EXPIRED,
                }:
                    raise RiskDecisionConflict(
                        f"risk decision delivery disposition changed: {decision.decision_id}"
                    )
''',
    '''                allowed_states = (
                    {
                        RiskDeliveryState.PENDING,
                        RiskDeliveryState.PUBLISHED,
                        RiskDeliveryState.EXPIRED,
                    }
                    if decision.status is RiskDecisionStatus.APPROVED
                    else {RiskDeliveryState.RECORDED}
                )
                if stored.delivery_state not in allowed_states:
                    raise RiskDecisionConflict(
                        f"risk decision delivery disposition changed: {decision.decision_id}"
                    )
''',
)
replace_once(
    "src/crypto_quant/risk_store.py",
    '''            row = connection.execute(
                "SELECT delivery_state FROM risk_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"risk decision not found: {decision_id}")
            state = RiskDeliveryState(row["delivery_state"])
            if state is RiskDeliveryState.PUBLISHED:
                return False
            if state is not RiskDeliveryState.PENDING:
                raise ValueError(f"{state.value} risk decision cannot be published")
            connection.execute(
''',
    '''            row = connection.execute(
                "SELECT * FROM risk_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"risk decision not found: {decision_id}")
            stored = self._stored_from_row(row)
            state = stored.delivery_state
            if state is RiskDeliveryState.PUBLISHED:
                return False
            if state is not RiskDeliveryState.PENDING:
                raise ValueError(f"{state.value} risk decision cannot be published")
            if published_at > stored.decision.valid_until:
                raise ValueError("risk decision expired before publication")
            connection.execute(
''',
)
replace_once(
    "src/crypto_quant/risk_store.py",
    '''            row = connection.execute(
                "SELECT delivery_state FROM risk_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"risk decision not found: {decision_id}")
            state = RiskDeliveryState(row["delivery_state"])
            if state is RiskDeliveryState.EXPIRED:
                return False
            if state is not RiskDeliveryState.PENDING:
                raise ValueError(f"{state.value} risk decision cannot expire")
            connection.execute(
''',
    '''            row = connection.execute(
                "SELECT * FROM risk_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"risk decision not found: {decision_id}")
            stored = self._stored_from_row(row)
            state = stored.delivery_state
            if state is RiskDeliveryState.EXPIRED:
                return False
            if state is not RiskDeliveryState.PENDING:
                raise ValueError(f"{state.value} risk decision cannot expire")
            if expired_at <= stored.decision.valid_until:
                raise ValueError("risk decision cannot expire before validity window ends")
            connection.execute(
''',
)
