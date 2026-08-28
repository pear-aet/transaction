import pytest

from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle, Signal
from crypto_quant.engine import EngineState, EventEngine

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


def test_risk_evaluator_value_error_is_not_mislabeled_as_invalid_strategy_intent() -> None:
    def broken_evaluator(_context):
        raise ValueError("risk evaluator bug")

    state = EngineState(10_000)
    engine = EventEngine(config(), state, risk_evaluator=broken_evaluator)
    state.pending[SYMBOL] = Signal(0, "enter_long", "valid_signal", 99)
    bar = Candle(0, 100, 101, 99.5, 100, 1)

    with pytest.raises(ValueError, match="risk evaluator bug"):
        engine.process_bar(SYMBOL, bar, [bar], [], generate_signal=False)

    assert state.positions == {}
    assert not any(
        event.get("reason") == "invalid_strategy_intent" for event in state.events
    )
