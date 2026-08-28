from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/crypto_quant/risk_contracts.py",
    '    STALE_INTENT = "stale_intent"\n    STALE_ACCOUNT = "stale_account"\n',
    '    STALE_INTENT = "stale_intent"\n    FUTURE_INTENT = "future_intent"\n    STALE_ACCOUNT = "stale_account"\n',
)
replace_once(
    "src/crypto_quant/risk_contracts.py",
    "    decision_time: int\n    evaluated_at: int\n    reasons: tuple[RiskReason, ...]\n",
    "    decision_time: int\n    evaluated_at: int\n    valid_until: int\n    reasons: tuple[RiskReason, ...]\n",
)
replace_once(
    "src/crypto_quant/risk_contracts.py",
    '''        counters = (self.decision_time, self.evaluated_at, self.sequence)
        if min(counters) < 0:
            raise ValueError("risk decision counters must be non-negative")
''',
    '''        counters = (self.decision_time, self.evaluated_at, self.valid_until, self.sequence)
        if min(counters) < 0:
            raise ValueError("risk decision counters must be non-negative")
        if self.valid_until < self.evaluated_at:
            raise ValueError("risk decision cannot expire before evaluation")
''',
)
replace_once(
    "src/crypto_quant/risk_contracts.py",
    '                "decision_time": self.decision_time,\n                "reasons": [reason.value for reason in self.reasons],\n',
    '                "decision_time": self.decision_time,\n                "valid_until": self.valid_until,\n                "reasons": [reason.value for reason in self.reasons],\n',
)
replace_once(
    "src/crypto_quant/risk_contracts.py",
    '            decision_time=int(value["decision_time"]),\n            evaluated_at=int(value["evaluated_at"]),\n            reasons=tuple(RiskReason(reason) for reason in value["reasons"]),\n',
    '            decision_time=int(value["decision_time"]),\n            evaluated_at=int(value["evaluated_at"]),\n            valid_until=int(value["valid_until"]),\n            reasons=tuple(RiskReason(reason) for reason in value["reasons"]),\n',
)

replace_once(
    "src/crypto_quant/risk.py",
    '''    leverage: float,
    stop_price: float | None,
) -> str:
''',
    '''    leverage: float,
    stop_price: float | None,
    valid_until: int,
) -> str:
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '            "stop_price": None if stop_price is None else format(stop_price, ".17g"),\n',
    '            "stop_price": None if stop_price is None else format(stop_price, ".17g"),\n            "valid_until": valid_until,\n',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''    resolved_reasons = _dedupe_reasons(reasons)
    market_price = context.market.mark_price if approval_price is None else approval_price
    decision_id = _decision_id(
''',
    '''    resolved_reasons = _dedupe_reasons(reasons)
    market_price = context.market.mark_price if approval_price is None else approval_price
    valid_until = context.evaluated_at
    decision_id = _decision_id(
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''        leverage=0.0,
        stop_price=context.intent.stop_price,
    )
''',
    '''        leverage=0.0,
        stop_price=context.intent.stop_price,
        valid_until=valid_until,
    )
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''        decision_time=context.intent.decision_time,
        evaluated_at=context.evaluated_at,
        reasons=resolved_reasons,
''',
    '''        decision_time=context.intent.decision_time,
        evaluated_at=context.evaluated_at,
        valid_until=valid_until,
        reasons=resolved_reasons,
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''    reasons = (RiskReason.APPROVED_EXIT,)
    decision_id = _decision_id(
''',
    '''    reasons = (RiskReason.APPROVED_EXIT,)
    valid_until = context.intent.decision_time + policy.max_intent_age_ms
    decision_id = _decision_id(
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''        leverage=1.0,
        stop_price=None,
    )
''',
    '''        leverage=1.0,
        stop_price=None,
        valid_until=valid_until,
    )
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''        decision_time=context.intent.decision_time,
        evaluated_at=context.evaluated_at,
        reasons=reasons,
''',
    '''        decision_time=context.intent.decision_time,
        evaluated_at=context.evaluated_at,
        valid_until=valid_until,
        reasons=reasons,
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''    warnings: list[str] = []
    if intent.action is StrategyAction.EXIT:
        position = account.position(intent.symbol)
''',
    '''    warnings: list[str] = []
    if intent.action is StrategyAction.EXIT:
        exit_reasons: list[RiskReason] = []
        if intent.decision_time > context.evaluated_at:
            exit_reasons.append(RiskReason.FUTURE_INTENT)
        if context.evaluated_at - intent.decision_time > policy.max_intent_age_ms:
            exit_reasons.append(RiskReason.STALE_INTENT)
        if exit_reasons:
            return _reject(context, exit_reasons)
        position = account.position(intent.symbol)
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''    if account.observed_at > context.evaluated_at or market.observed_at > context.evaluated_at:
        reasons.append(RiskReason.FUTURE_SNAPSHOT)
    if context.evaluated_at - intent.decision_time > policy.max_intent_age_ms:
''',
    '''    if account.observed_at > context.evaluated_at or market.observed_at > context.evaluated_at:
        reasons.append(RiskReason.FUTURE_SNAPSHOT)
    if intent.decision_time > context.evaluated_at:
        reasons.append(RiskReason.FUTURE_INTENT)
    if context.evaluated_at - intent.decision_time > policy.max_intent_age_ms:
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''    action = RiskApprovedAction.OPEN_LONG if side == "long" else RiskApprovedAction.OPEN_SHORT
    reasons_tuple = (RiskReason.APPROVED_ENTRY,)
    decision_id = _decision_id(
''',
    '''    action = RiskApprovedAction.OPEN_LONG if side == "long" else RiskApprovedAction.OPEN_SHORT
    reasons_tuple = (RiskReason.APPROVED_ENTRY,)
    valid_until = min(
        intent.decision_time + policy.max_intent_age_ms,
        account.observed_at + policy.max_account_age_ms,
        market.observed_at + policy.max_market_age_ms,
    )
    decision_id = _decision_id(
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''        leverage=selected.leverage,
        stop_price=stop_price,
    )
''',
    '''        leverage=selected.leverage,
        stop_price=stop_price,
        valid_until=valid_until,
    )
''',
)
replace_once(
    "src/crypto_quant/risk.py",
    '''        decision_time=intent.decision_time,
        evaluated_at=context.evaluated_at,
        reasons=reasons_tuple,
''',
    '''        decision_time=intent.decision_time,
        evaluated_at=context.evaluated_at,
        valid_until=valid_until,
        reasons=reasons_tuple,
''',
)
