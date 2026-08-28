from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


# Durable replay cursors must never advance past mutable pending rows.
replace_once(
    "src/crypto_quant/strategy_replay.py",
    '''    def __post_init__(self) -> None:
        if not self.delivery_states:
            raise ValueError("durable intent filter requires at least one delivery state")
''',
    '''    def __post_init__(self) -> None:
        if not self.delivery_states:
            raise ValueError("durable intent filter requires at least one delivery state")
        if StrategyDeliveryState.PENDING in self.delivery_states:
            raise ValueError("durable intent replay cannot include mutable pending state")
''',
)
replace_once(
    "src/crypto_quant/strategy_replay.py",
    '''        scanned_through = (
            offset.sequence
            if not rows
            else int(rows[-1].intent.storage_sequence or offset.sequence)
        )
        matched = tuple(item for item in rows if selected_filter.matches(item))
        return StrategyReplayBatch(
            consumer,
            offset.filter_hash,
            offset.sequence,
            high_watermark,
            scanned_through,
            matched,
            len(rows) - len(matched),
        )
''',
    '''        scanned_through = offset.sequence
        matched = []
        skipped = 0
        for item in rows:
            if item.delivery_state is StrategyDeliveryState.PENDING:
                break
            storage_sequence = item.intent.storage_sequence
            if storage_sequence is None:
                raise RuntimeError("stored strategy intent has no storage sequence")
            scanned_through = int(storage_sequence)
            if selected_filter.matches(item):
                matched.append(item)
            else:
                skipped += 1
        return StrategyReplayBatch(
            consumer,
            offset.filter_hash,
            offset.sequence,
            high_watermark,
            scanned_through,
            tuple(matched),
            skipped,
        )
''',
)

replace_once(
    "src/crypto_quant/risk_replay.py",
    '''    def __post_init__(self) -> None:
        if not self.delivery_states:
            raise ValueError("durable risk filter requires at least one delivery state")
''',
    '''    def __post_init__(self) -> None:
        if not self.delivery_states:
            raise ValueError("durable risk filter requires at least one delivery state")
        if RiskDeliveryState.PENDING in self.delivery_states:
            raise ValueError("durable risk replay cannot include mutable pending state")
''',
)
replace_once(
    "src/crypto_quant/risk_replay.py",
    '''        scanned_through = (
            offset.sequence
            if not rows
            else int(rows[-1].decision.storage_sequence or offset.sequence)
        )
        matched = tuple(item for item in rows if selected_filter.matches(item))
        return RiskReplayBatch(
            consumer,
            offset.filter_hash,
            offset.sequence,
            high_watermark,
            scanned_through,
            matched,
            len(rows) - len(matched),
        )
''',
    '''        scanned_through = offset.sequence
        matched = []
        skipped = 0
        for item in rows:
            if item.delivery_state is RiskDeliveryState.PENDING:
                break
            storage_sequence = item.decision.storage_sequence
            if storage_sequence is None:
                raise RuntimeError("stored risk decision has no storage sequence")
            scanned_through = int(storage_sequence)
            if selected_filter.matches(item):
                matched.append(item)
            else:
                skipped += 1
        return RiskReplayBatch(
            consumer,
            offset.filter_hash,
            offset.sequence,
            high_watermark,
            scanned_through,
            tuple(matched),
            skipped,
        )
''',
)

# Every evaluator result must remain pinned to the exact input context.
replace_once(
    "src/crypto_quant/risk.py",
    '''    return True


def _decision_id(
''',
    '''    return True


class RiskDecisionMismatch(RuntimeError):
    """Raised when an evaluator returns a decision for another input context."""


def validate_risk_decision(context: RiskContext, decision: RiskDecision) -> RiskDecision:
    expected_policy_hash = risk_policy_hash(context.policy)
    checks = (
        ("engine_instance_id", decision.engine_instance_id, context.engine_instance_id),
        ("policy_id", decision.policy_id, context.policy.policy_id),
        ("policy_version", decision.policy_version, context.policy.version),
        ("policy_hash", decision.policy_hash, expected_policy_hash),
        ("account_id", decision.account_id, context.account.account_id),
        ("account_snapshot_id", decision.account_snapshot_id, context.account.snapshot_id),
        ("market_snapshot_id", decision.market_snapshot_id, context.market.snapshot_id),
        ("intent_id", decision.intent_id, context.intent.intent_id),
        (
            "intent_storage_sequence",
            decision.intent_storage_sequence,
            context.intent.storage_sequence,
        ),
        ("symbol", decision.symbol, context.intent.symbol),
        ("requested_action", decision.requested_action, context.intent.action),
        ("decision_time", decision.decision_time, context.intent.decision_time),
        ("evaluated_at", decision.evaluated_at, context.evaluated_at),
    )
    mismatches = [name for name, actual, expected in checks if actual != expected]
    if not math.isclose(
        decision.reference_price,
        context.intent.reference_price,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        mismatches.append("reference_price")
    if mismatches:
        raise RiskDecisionMismatch(
            "risk decision provenance mismatch: " + ", ".join(mismatches)
        )
    return decision


def _decision_id(
''',
)

replace_once(
    "src/crypto_quant/risk_engine.py",
    "from .risk import evaluate_risk\n",
    "from .risk import evaluate_risk, validate_risk_decision\n",
)
replace_once(
    "src/crypto_quant/risk_engine.py",
    '''            try:
                await self._assert_execution_subscriber()
                published, expired = await self._flush_outbox(current)
                self.health.outbox_recoveries += published
''',
    '''            try:
                published, expired = await self._flush_outbox(current)
                await self._assert_execution_subscriber()
                self.health.outbox_recoveries += published
''',
)
replace_once(
    "src/crypto_quant/risk_engine.py",
    '''                decision = await asyncio.to_thread(self.evaluator, context)
                self.health.evaluations += 1
                commit = await asyncio.to_thread(
''',
    '''                decision = await asyncio.to_thread(self.evaluator, context)
                validate_risk_decision(context, decision)
                self.health.evaluations += 1
                commit = await asyncio.to_thread(
''',
)

replace_once(
    "src/crypto_quant/engine.py",
    "from .risk import evaluate_risk\n",
    "from .risk import evaluate_risk, validate_risk_decision\n",
)
replace_once(
    "src/crypto_quant/engine.py",
    '''    def _entry_risk_decision(
        self,
        symbol: str,
        intent: StrategyIntent,
        bar: Candle,
    ) -> RiskDecision:
        return self.risk_evaluator(
            RiskContext(
                "event-engine-risk-v1",
                intent,
                self._entry_account_snapshot(symbol, bar),
                self._entry_market_snapshot(symbol, bar),
                RiskPolicy.from_strategy_config(self.config),
                bar.timestamp,
            )
        )
''',
    '''    def _entry_risk_decision(
        self,
        symbol: str,
        intent: StrategyIntent,
        bar: Candle,
    ) -> RiskDecision:
        context = RiskContext(
            "event-engine-risk-v1",
            intent,
            self._entry_account_snapshot(symbol, bar),
            self._entry_market_snapshot(symbol, bar),
            RiskPolicy.from_strategy_config(self.config),
            bar.timestamp,
        )
        decision = self.risk_evaluator(context)
        return validate_risk_decision(context, decision)
''',
)

# Strengthen execution-plan mathematical invariants.
replace_once(
    "src/crypto_quant/risk_contracts.py",
    '''        if min(nonnegative) < 0:
            raise ValueError("risk decision amounts must be non-negative")
        if self.status is RiskDecisionStatus.APPROVED:
''',
    '''        if min(nonnegative) < 0:
            raise ValueError("risk decision amounts must be non-negative")
        if self.status is RiskDecisionStatus.APPROVED:
            expected_notional = self.quantity * self.approval_price
            if not math.isclose(
                self.notional,
                expected_notional,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                raise ValueError("approved notional must equal quantity times approval price")
''',
)
replace_once(
    "src/crypto_quant/risk_contracts.py",
    '''                if RiskReason.APPROVED_ENTRY not in self.reasons:
                    raise ValueError("approved entry requires the approved_entry reason")
                if self.reduce_only or self.required_margin <= 0 or self.stop_price is None:
                    raise ValueError(
                        "approved entry requires margin, stop and non-reduce-only plan"
                    )
''',
    '''                if self.reasons != (RiskReason.APPROVED_ENTRY,):
                    raise ValueError("approved entry requires only the approved_entry reason")
                if self.reduce_only or self.required_margin <= 0 or self.stop_price is None:
                    raise ValueError(
                        "approved entry requires margin, stop and non-reduce-only plan"
                    )
                if self.approved_action is RiskApprovedAction.OPEN_LONG:
                    if self.stop_price >= self.approval_price:
                        raise ValueError("approved long stop must remain below approval price")
                elif self.stop_price <= self.approval_price:
                    raise ValueError("approved short stop must remain above approval price")
                minimum_margin = self.notional / self.leverage
                if self.required_margin + 1e-9 < minimum_margin:
                    raise ValueError("approved entry margin is below notional divided by leverage")
                if self.initial_risk > self.risk_budget + max(1e-9, self.risk_budget * 1e-9):
                    raise ValueError("approved entry initial risk exceeds its risk budget")
                if min(
                    self.symbol_risk_after,
                    self.portfolio_risk_after,
                    self.group_risk_after,
                ) + 1e-9 < self.initial_risk:
                    raise ValueError("approved entry risk-after values omit the new initial risk")
                if min(self.gross_notional_after, self.directional_notional_after) + 1e-9 < self.notional:
                    raise ValueError("approved entry notional-after values omit the new notional")
''',
)
replace_once(
    "src/crypto_quant/risk_contracts.py",
    '''                if RiskReason.APPROVED_EXIT not in self.reasons:
                    raise ValueError("approved exit requires the approved_exit reason")
                if not self.reduce_only or self.required_margin != 0 or self.stop_price is not None:
                    raise ValueError("approved exit must be reduce-only without new margin or stop")
''',
    '''                if self.reasons != (RiskReason.APPROVED_EXIT,):
                    raise ValueError("approved exit requires only the approved_exit reason")
                if not self.reduce_only or self.required_margin != 0 or self.stop_price is not None:
                    raise ValueError("approved exit must be reduce-only without new margin or stop")
                if self.leverage != 1 or self.risk_budget != 0 or self.initial_risk != 0:
                    raise ValueError("approved exit cannot add leverage or new risk")
''',
)
