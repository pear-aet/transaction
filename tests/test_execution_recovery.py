import asyncio

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.execution import build_execution_command
from crypto_quant.execution_contracts import (
    ExecutionContext,
    ExecutionMarketSnapshot,
    ExecutionMode,
    ExecutionPolicy,
)
from crypto_quant.execution_engine import ExecutionEngine, ExecutionEngineSettings
from crypto_quant.execution_store import ExecutionStore
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskPolicy,
)
from crypto_quant.risk_store import RiskStore
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
NOW = 900_001


def risk_rules() -> MarketRules:
    return MarketRules(
        SYMBOL,
        0.1,
        0.001,
        0.001,
        5,
        100,
        (RiskBracket(0, 1_000_000, 0.005),),
    )


def persisted_decision(store: RiskStore):
    intent = StrategyIntent(
        intent_id="execution-recovery-intent",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=900_000,
        created_at=900_000,
        reference_price=100,
        stop_price=99,
        reason="execution_recovery_test",
        source_event_id="execution-recovery-market",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=1,
    )
    account = AccountRiskSnapshot(
        "execution-recovery-account",
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
        "execution-recovery-risk-market",
        SYMBOL,
        900_000,
        100,
        risk_rules(),
    )
    decision = evaluate_risk(
        RiskContext("risk-primary", intent, account, market, RiskPolicy(), NOW)
    )
    commit = store.commit_evaluation(decision, updated_at=NOW)
    stored = commit.stored_decision.decision
    store.mark_published(stored.decision_id, NOW)
    return stored


def execution_market(decision, now_ms: int) -> ExecutionMarketSnapshot:
    return ExecutionMarketSnapshot(
        f"execution-recovery-market:{decision.decision_id}",
        SYMBOL,
        now_ms,
        100,
        99.9,
        100.1,
    )


class NoopBroker:
    network_write_enabled = False

    def dispatch(self, *args, **kwargs):
        return ()


def test_bootstrap_rescans_dispatched_commands_for_terminal_reconciliation(tmp_path) -> None:
    async def scenario() -> None:
        database = tmp_path / "quant.db"
        risk_store = RiskStore(database)
        decision = persisted_decision(risk_store)
        execution_store = ExecutionStore(database)
        policy = ExecutionPolicy(mode=ExecutionMode.SIMULATION)
        command = build_execution_command(
            ExecutionContext(
                "execution-primary",
                decision,
                execution_market(decision, NOW),
                policy,
                NOW,
            )
        )
        execution_store.commit_command(command, updated_at=NOW)
        execution_store.mark_dispatched(command.command_id, NOW)
        assert [item.command.command_id for item in execution_store.active_commands()] == [
            command.command_id
        ]

        engine = ExecutionEngine(
            ExecutionEngineSettings("execution-primary", "paper"),
            policy,
            execution_market,
            store=execution_store,
            risk_store=risk_store,
            broker=NoopBroker(),
        )
        calls = []

        async def reconcile_probe(*args):
            calls.append(args)
            return None

        engine._reconcile_command = reconcile_probe
        await engine.bootstrap(now_ms=NOW)

        assert len(calls) == 1

    asyncio.run(scenario())
