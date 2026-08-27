from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    exchange: str = "binanceusdm"
    symbol: str = "BTC/USDT:USDT"
    market_type: str = "swap"
    margin_mode: str = "cross"
    max_leverage: float = 100.0
    high_timeframe: str = "1h"
    low_timeframe: str = "15m"
    initial_cash: float = 10_000.0
    risk_per_trade: float = 0.01
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    stop_buffer: float = 0.001
    reward_to_risk: float = 2.0
    min_stroke_separation: int = 4
    fallback_funding_rate_8h: float = 0.0001
    maintenance_margin_rate: float = 0.005
    liquidation_fee_rate: float = 0.005
    entry_model: str = "chan_fractal"
    entry_lookback: int = 20
    daily_loss_limit: float = 0.03
    max_account_drawdown: float = 0.25
    max_consecutive_losses: int = 5
    max_portfolio_risk: float = 0.03
    max_symbol_risk: float = 0.015
    max_directional_notional: float = 20.0
    min_liquidation_buffer: float = 0.02
    require_mark_price: bool = True
    data_dir: str = "data"

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if not 0 < self.risk_per_trade <= 0.05:
            raise ValueError("risk_per_trade must be in (0, 0.05]")
        if not 0 <= self.fee_rate < 0.02 or not 0 <= self.slippage_rate < 0.02:
            raise ValueError("fee/slippage rates are outside safe research bounds")
        if self.reward_to_risk <= 0 or self.min_stroke_separation < 1:
            raise ValueError("invalid strategy parameter")
        if self.market_type != "swap" or self.margin_mode != "cross":
            raise ValueError("v0.1 only supports cross-margined perpetual swaps")
        if not 1 <= self.max_leverage <= 100:
            raise ValueError("max_leverage must be in [1, 100]")
        if abs(self.fallback_funding_rate_8h) >= 0.01:
            raise ValueError("fallback funding rate is implausibly large")
        if not 0 < self.maintenance_margin_rate < 0.1:
            raise ValueError("invalid maintenance margin rate")
        if self.entry_model not in {
            "chan_fractal",
            "chan_third_point",
            "donchian",
            "ema_pullback",
        }:
            raise ValueError("unsupported entry_model")
        if self.entry_lookback < 5:
            raise ValueError("entry_lookback must be >= 5")
        if not 0 < self.daily_loss_limit <= 0.1:
            raise ValueError("daily_loss_limit must be in (0, 0.1]")
        if not 0 < self.max_account_drawdown <= 0.5:
            raise ValueError("max_account_drawdown must be in (0, 0.5]")
        if not 1 <= self.max_consecutive_losses <= 20:
            raise ValueError("max_consecutive_losses must be in [1, 20]")
        if not 0 < self.max_symbol_risk <= self.max_portfolio_risk <= 0.1:
            raise ValueError("symbol/portfolio risk limits are inconsistent")
        if not 0 < self.max_directional_notional <= 100:
            raise ValueError("max_directional_notional must be in (0, 100]")
        if not 0.005 <= self.min_liquidation_buffer <= 0.25:
            raise ValueError("min_liquidation_buffer must be in [0.005, 0.25]")

    @classmethod
    def from_env(cls) -> StrategyConfig:
        return cls(
            exchange=os.getenv("CQ_EXCHANGE", "binanceusdm"),
            symbol=os.getenv("CQ_SYMBOL", "BTC/USDT:USDT"),
            max_leverage=float(os.getenv("CQ_MAX_LEVERAGE", "100")),
            high_timeframe=os.getenv("CQ_HIGH_TIMEFRAME", "1h"),
            low_timeframe=os.getenv("CQ_LOW_TIMEFRAME", "15m"),
            initial_cash=float(os.getenv("CQ_INITIAL_CASH", "10000")),
            risk_per_trade=float(os.getenv("CQ_RISK_PER_TRADE", "0.01")),
            fee_rate=float(os.getenv("CQ_FEE_RATE", "0.001")),
            slippage_rate=float(os.getenv("CQ_SLIPPAGE_RATE", "0.0005")),
            daily_loss_limit=float(os.getenv("CQ_DAILY_LOSS_LIMIT", "0.03")),
            max_account_drawdown=float(os.getenv("CQ_MAX_ACCOUNT_DRAWDOWN", "0.25")),
            max_consecutive_losses=int(os.getenv("CQ_MAX_CONSECUTIVE_LOSSES", "5")),
            max_portfolio_risk=float(os.getenv("CQ_MAX_PORTFOLIO_RISK", "0.03")),
            max_symbol_risk=float(os.getenv("CQ_MAX_SYMBOL_RISK", "0.015")),
            max_directional_notional=float(os.getenv("CQ_MAX_DIRECTIONAL_NOTIONAL", "20")),
            min_liquidation_buffer=float(os.getenv("CQ_MIN_LIQUIDATION_BUFFER", "0.02")),
            require_mark_price=os.getenv("CQ_REQUIRE_MARK_PRICE", "true").lower()
            in {"1", "true", "yes"},
            data_dir=os.getenv("CQ_DATA_DIR", "data"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
