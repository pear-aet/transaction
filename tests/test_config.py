import pytest

from crypto_quant.config import StrategyConfig


def test_contract_scope_is_locked() -> None:
    config = StrategyConfig()
    assert config.exchange == "binanceusdm"
    assert config.market_type == "swap"
    assert config.margin_mode == "cross"
    assert config.high_timeframe == "1h"
    assert config.low_timeframe == "15m"


@pytest.mark.parametrize("value", [0, 101])
def test_leverage_cap(value: float) -> None:
    with pytest.raises(ValueError):
        StrategyConfig(max_leverage=value)
