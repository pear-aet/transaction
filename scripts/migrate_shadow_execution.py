from pathlib import Path


shadow_path = Path("src/crypto_quant/shadow.py")
text = shadow_path.read_text(encoding="utf-8")
marker = "def build_execution_shadow_command("
if marker not in text:
    text += '''

# Execution Engine compatibility boundary. New shadow planning must use the
# same immutable command contract as simulation and later exchange adapters.
from .execution import build_execution_command as _build_execution_command
from .execution_contracts import (
    ExecutionCommand as _ExecutionCommand,
    ExecutionContext as _ExecutionContext,
    ExecutionMarketSnapshot as _ExecutionMarketSnapshot,
    ExecutionMode as _ExecutionMode,
    ExecutionPolicy as _ExecutionPolicy,
)
from .risk_contracts import RiskDecision as _RiskDecision


def build_execution_shadow_command(
    decision: _RiskDecision,
    market: _ExecutionMarketSnapshot,
    *,
    engine_instance_id: str = "shadow-compat-v1",
    evaluated_at: int | None = None,
) -> _ExecutionCommand:
    """Plan a no-network shadow command through the canonical Execution planner."""

    current = market.observed_at if evaluated_at is None else evaluated_at
    return _build_execution_command(
        _ExecutionContext(
            engine_instance_id,
            decision,
            market,
            _ExecutionPolicy(mode=_ExecutionMode.SHADOW),
            current,
        )
    )
'''
    shadow_path.write_text(text, encoding="utf-8")


test_path = Path("tests/test_execution_shadow_compat.py")
test_path.write_text(
    '''from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution import build_execution_command
from crypto_quant.execution_contracts import (
    ExecutionCommandStatus,
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
)
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskPolicy,
)
from crypto_quant.shadow import build_execution_shadow_command
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
NOW = 900_001


def approved_decision():
    rules = MarketRules(
        SYMBOL,
        0.1,
        0.001,
        0.001,
        5,
        100,
        (RiskBracket(0, 1_000_000, 0.005),),
    )
    intent = StrategyIntent(
        intent_id="shadow-compat-intent",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=900_000,
        created_at=900_000,
        reference_price=100,
        stop_price=99,
        reason="shadow_compat_test",
        source_event_id="shadow-compat-market",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=1,
    )
    account = AccountRiskSnapshot(
        "shadow-compat-account",
        "paper",
        900_000,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    market = MarketRiskSnapshot(
        "shadow-compat-risk-market",
        SYMBOL,
        900_000,
        100,
        rules,
    )
    return evaluate_risk(
        RiskContext("risk-primary", intent, account, market, RiskPolicy(), NOW)
    ).with_storage_sequence(1)


def test_legacy_shadow_adapter_uses_the_canonical_execution_planner() -> None:
    decision = approved_decision()
    market = ExecutionMarketSnapshot(
        "shadow-compat-execution-market",
        SYMBOL,
        NOW,
        100,
        99.9,
        100.1,
    )

    compatibility = build_execution_shadow_command(
        decision,
        market,
        engine_instance_id="shadow-compat-v1",
        evaluated_at=NOW,
    )
    canonical = build_execution_command(
        ExecutionContext(
            "shadow-compat-v1",
            decision,
            market,
            ExecutionPolicy(mode=ExecutionMode.SHADOW),
            NOW,
        )
    )

    assert compatibility == canonical
    assert compatibility.status is ExecutionCommandStatus.SHADOW_RECORDED
    assert compatibility.network_write is False
    assert all(order.submitted is False for order in compatibility.orders)
    assert all(order.network_write is False for order in compatibility.orders)
''',
    encoding="utf-8",
)


architecture_path = Path("docs/ARCHITECTURE.md")
architecture = architecture_path.read_text(encoding="utf-8")
section_marker = "## Execution Engine stage"
if section_marker not in architecture:
    architecture += '''

## Execution Engine stage

The current execution stage consumes only persisted, published and unexpired
`RiskDecision` records. It creates deterministic `ExecutionCommand` records,
persists the parent command and every order atomically, and dispatches only to
no-network shadow or deterministic local-simulation brokers.

```text
RiskDecision log
    -> ExecutionRuntime durable catch-up
    -> ExecutionEngine source revalidation
    -> ExecutionStore command and order transaction
    -> serialized local outbox dispatch
    -> append-only OrderUpdate journal
    -> startup command reconciliation
```

Legacy `shadow.py` planning is retained only as a compatibility wrapper around
the canonical Execution planner. It no longer defines an independent position
size, leverage, margin or order-plan calculation.

The stage still has no authenticated exchange adapter, order submission,
amendment or cancellation endpoint. Live trading remains prohibited.
'''
    architecture_path.write_text(architecture, encoding="utf-8")
