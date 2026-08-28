from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle, Signal
from crypto_quant.engine import EngineState, EventEngine
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import RiskDecisionStatus, RiskReason

SYMBOL = "BTC/USDT:USDT"


def config() -> StrategyConfig:
    return StrategyConfig(
        symbol=SYMBOL,
        high_timeframe="1h",
        low_timeframe="15m",
        fee_rate=0,
        slippage_rate=0,
        fallback_funding_rate_8h=0,
        require_mark_price=False,
    )


def entry_bar(timestamp: int = 0) -> Candle:
    return Candle(timestamp, 100, 101, 99.5, 100, 1)


def test_event_engine_executes_the_same_pure_risk_decision() -> None:
    captured = []

    def evaluator(context):
        decision = evaluate_risk(context)
        captured.append((context, decision))
        return decision

    state = EngineState(10_000)
    engine = EventEngine(config(), state, risk_evaluator=evaluator)
    state.pending[SYMBOL] = Signal(0, "enter_long", "integration", 99)
    bar = entry_bar()

    engine.process_bar(SYMBOL, bar, [bar], [], generate_signal=False)

    assert len(captured) == 1
    context, decision = captured[0]
    assert decision.status is RiskDecisionStatus.APPROVED
    assert evaluate_risk(context) == decision
    position = state.positions[SYMBOL]
    assert position.quantity == decision.quantity
    assert position.entry_price == decision.approval_price
    assert position.stop_price == decision.stop_price
    assert position.initial_risk == decision.initial_risk
    opened = next(event for event in state.events if event["kind"] == "position_opened")
    assert opened["risk_decision_id"] == decision.decision_id
    assert opened["approved_leverage"] == decision.leverage


def test_event_engine_entry_halt_is_decided_by_risk_engine() -> None:
    state = EngineState(10_000, global_stop=True)
    engine = EventEngine(config(), state)
    state.pending[SYMBOL] = Signal(0, "enter_long", "halted", 99)
    bar = entry_bar()

    engine.process_bar(SYMBOL, bar, [bar], [], generate_signal=False)

    assert state.positions == {}
    rejected = next(event for event in state.events if event["kind"] == "entry_rejected")
    assert rejected["reason"] == RiskReason.ACCOUNT_GLOBAL_HALT.value
    assert RiskReason.ACCOUNT_GLOBAL_HALT.value in rejected["reasons"]
    assert rejected["risk_decision_id"].startswith("risk_decision:")


def test_event_engine_invalid_stop_uses_risk_rejection_contract() -> None:
    state = EngineState(10_000)
    engine = EventEngine(config(), state)
    state.pending[SYMBOL] = Signal(0, "enter_long", "bad_stop", 101)
    bar = entry_bar()

    engine.process_bar(SYMBOL, bar, [bar], [], generate_signal=False)

    assert state.positions == {}
    rejected = next(event for event in state.events if event["kind"] == "entry_rejected")
    assert rejected["reason"] == RiskReason.INVALID_STOP.value
