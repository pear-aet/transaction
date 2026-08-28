import pytest

from crypto_quant.config import StrategyConfig
from crypto_quant.domain import MarketRules, RiskBracket, Signal
from crypto_quant.engine import EngineState
from crypto_quant.execution import build_execution_command
from crypto_quant.execution_contracts import (
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
    RiskDecisionStatus,
    RiskPolicy,
)
from crypto_quant.shadow import execution_shadow_report, shadow_report
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
NOW = 900_001


def rules() -> MarketRules:
    return MarketRules(
        SYMBOL,
        tick_size=0.1,
        step_size=0.001,
        min_quantity=0.001,
        min_notional=5,
        max_leverage=100,
        brackets=(RiskBracket(0, 1_000_000, 0.005),),
    )


def shadow_command(*, persisted: bool = True, simulation: bool = False):
    intent = StrategyIntent(
        intent_id="intent-shadow",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=900_000,
        created_at=900_000,
        reference_price=100,
        stop_price=99,
        reason="shadow_compatibility_test",
        source_event_id="market-shadow",
        source_sequence=1,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=1,
    )
    account = AccountRiskSnapshot(
        "account-shadow",
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
        "market-shadow",
        SYMBOL,
        900_000,
        100,
        rules(),
    )
    decision = evaluate_risk(
        RiskContext(
            "risk-primary",
            intent,
            account,
            market,
            RiskPolicy(),
            NOW,
        )
    )
    assert decision.status is RiskDecisionStatus.APPROVED
    mode = ExecutionMode.SIMULATION if simulation else ExecutionMode.SHADOW
    command = build_execution_command(
        ExecutionContext(
            "execution-primary",
            decision.with_storage_sequence(1),
            ExecutionMarketSnapshot(
                "execution-market-shadow",
                SYMBOL,
                NOW,
                100,
                99.9,
                100.1,
            ),
            ExecutionPolicy(mode=mode),
            NOW,
        )
    )
    return command.with_storage_sequence(1) if persisted else command


def test_legacy_shadow_report_refuses_to_label_raw_signals_as_orders() -> None:
    state = EngineState(StrategyConfig().initial_cash)
    state.pending[SYMBOL] = Signal(900_000, "enter_long", "legacy", 99)

    report = shadow_report(state, {SYMBOL: rules()})

    assert report["source"] == "legacy_strategy_signals"
    assert report["risk_approved"] is False
    assert report["executable"] is False
    assert report["submission_enabled"] is False
    assert report["network_write_enabled"] is False
    assert report["orders"] == []
    assert report["legacy_intents"][0]["validation"] == "unapproved_strategy_signal"
    assert report["legacy_intents"][0]["risk_approved"] is False


def test_formal_shadow_report_uses_persisted_risk_approved_execution_plan() -> None:
    command = shadow_command()

    report = execution_shadow_report([command])

    assert report["source"] == "persisted_execution_commands"
    assert report["risk_approved"] is True
    assert report["submission_enabled"] is False
    assert report["network_write_enabled"] is False
    assert report["command_count"] == 1
    assert report["order_count"] == 2
    primary, protective = report["orders"]
    assert primary["command_id"] == command.command_id
    assert primary["source_decision_id"] == command.source_decision_id
    assert primary["quantity"] == command.quantity
    assert primary["risk_approved"] is True
    assert primary["executable_plan"] is True
    assert primary["submitted"] is False
    assert primary["network_write"] is False
    assert protective["reduce_only"] is True
    assert protective["trigger_price"] == command.stop_price


def test_formal_shadow_report_rejects_unpersisted_or_simulation_commands() -> None:
    with pytest.raises(ValueError, match="persisted execution commands"):
        execution_shadow_report([shadow_command(persisted=False)])
    with pytest.raises(ValueError, match="shadow commands only"):
        execution_shadow_report([shadow_command(simulation=True)])
