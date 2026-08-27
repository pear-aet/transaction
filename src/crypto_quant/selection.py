from __future__ import annotations

import random
from dataclasses import dataclass, replace
from statistics import fmean, median, pstdev
from typing import Any

from .backtest import BacktestResult, run_backtest
from .config import StrategyConfig
from .domain import Candle, FundingRate, MarketRules, finite_json
from .strategy import strategy_registry

DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class CandidateScore:
    strategy_id: str
    version: str
    lookback: int
    score: float | None
    validation_returns: tuple[float, ...]
    median_validation_return: float
    worst_validation_drawdown: float
    median_validation_sharpe: float
    total_validation_trades: int
    eligible: bool

    @property
    def entry_model(self) -> str:
        return self.strategy_id


@dataclass(frozen=True, slots=True)
class SelectionResult:
    symbol: str
    selected: CandidateScore | None
    leaderboard: tuple[CandidateScore, ...]
    folds: int
    final_test: dict[str, Any] | None
    stress_test: dict[str, Any] | None
    methodology: str = "expanding_walk_forward_with_untouched_final_test"

    @property
    def windows(self) -> int:
        return self.folds

    def to_dict(self) -> dict[str, Any]:
        def item(value: CandidateScore) -> dict[str, Any]:
            return {
                "entry_model": value.strategy_id,
                "strategy_id": value.strategy_id,
                "version": value.version,
                "lookback": value.lookback,
                "score": value.score,
                "validation_returns": value.validation_returns,
                "median_return": value.median_validation_return,
                "worst_drawdown": value.worst_validation_drawdown,
                "median_sharpe": value.median_validation_sharpe,
                "total_trades": value.total_validation_trades,
                "eligible": value.eligible,
            }

        return finite_json(
            {
                "symbol": self.symbol,
                "selected": item(self.selected) if self.selected else None,
                "leaderboard": [item(value) for value in self.leaderboard],
                "windows": self.folds,
                "folds": self.folds,
                "final_test": self.final_test,
                "stress_test": self.stress_test,
                "methodology": self.methodology,
            }
        )


def _segments(
    candles: list[Candle], fold_count: int
) -> tuple[list[tuple[list[Candle], list[Candle]]], list[Candle]]:
    if fold_count < 2:
        raise ValueError("fold_count must be at least 2")
    if len(candles) < 500:
        raise ValueError("need at least 500 low-timeframe candles for walk-forward selection")
    final_start = int(len(candles) * 0.8)
    development = candles[:final_start]
    final_test = candles[max(0, final_start - 100) :]
    validation_size = max(60, (len(development) - 200) // fold_count)
    folds: list[tuple[list[Candle], list[Candle]]] = []
    for index in range(fold_count):
        validation_start = 200 + index * validation_size
        validation_end = min(len(development), validation_start + validation_size)
        if validation_end - validation_start < 50:
            continue
        train = development[:validation_start]
        validation = development[max(0, validation_start - 100) : validation_end]
        folds.append((train, validation))
    if len(folds) < 2:
        raise ValueError("history cannot form at least two complete walk-forward folds")
    return folds, final_test


def _slice_related(
    low: list[Candle], high: list[Candle], funding: list[FundingRate]
) -> tuple[list[Candle], list[FundingRate]]:
    start, end = low[0].timestamp, low[-1].timestamp
    high_slice = [item for item in high if start - 60 * DAY_MS <= item.timestamp <= end]
    funding_slice = [item for item in funding if start <= item.timestamp <= end]
    return high_slice, funding_slice


def _evaluate(
    low: list[Candle],
    high: list[Candle],
    funding: list[FundingRate],
    config: StrategyConfig,
    rules: MarketRules | None,
) -> BacktestResult | None:
    high_slice, funding_slice = _slice_related(low, high, funding)
    try:
        return run_backtest(low, high_slice, config, funding_slice, market_rules=rules)
    except ValueError:
        return None


def _monte_carlo_drawdown(result: BacktestResult, samples: int = 500) -> float:
    pnl = [trade.net_pnl for trade in result.trades]
    if not pnl:
        return 1.0
    generator = random.Random(20260827)
    drawdowns: list[float] = []
    for _ in range(samples):
        shuffled = generator.sample(pnl, len(pnl))
        equity = result.initial_cash
        peak = equity
        worst = 0.0
        for value in shuffled:
            equity += value
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak if peak > 0 else 1.0)
        drawdowns.append(worst)
    return sorted(drawdowns)[int(len(drawdowns) * 0.95)]


def _regime_metrics(candles: list[Candle], result: BacktestResult) -> dict[str, Any]:
    """Attribute final-test trades to deterministic trend/volatility regimes."""
    regimes: dict[int, str] = {}
    for index, candle in enumerate(candles):
        if index < 20:
            regimes[candle.timestamp] = "warmup"
            continue
        prior = candles[index - 20]
        trend = candle.close / prior.close - 1 if prior.close else 0.0
        ranges = [(item.high - item.low) / item.close for item in candles[index - 20 : index + 1]]
        volatility = fmean(ranges)
        if volatility >= 0.04:
            regime = "high_volatility"
        elif trend > 0.03:
            regime = "bull"
        elif trend < -0.03:
            regime = "bear"
        else:
            regime = "sideways"
        regimes[candle.timestamp] = regime
    timestamps = sorted(regimes)
    output: dict[str, dict[str, float | int]] = {}
    for trade in result.trades:
        eligible = [timestamp for timestamp in timestamps if timestamp <= trade.entry_time]
        regime = regimes[eligible[-1]] if eligible else "warmup"
        item = output.setdefault(regime, {"trades": 0, "net_pnl": 0.0})
        item["trades"] = int(item["trades"]) + 1
        item["net_pnl"] = float(item["net_pnl"]) + trade.net_pnl
    return output


def select_strategy_for_symbol(
    low_candles: list[Candle],
    high_candles: list[Candle],
    base: StrategyConfig,
    funding_rates: list[FundingRate] | None = None,
    window_count: int = 3,
    min_total_trades: int = 12,
    max_allowed_drawdown: float = 0.35,
    market_rules: MarketRules | None = None,
) -> SelectionResult:
    """Select on validation folds, then evaluate the winner once on final test data."""
    folds, final_test = _segments(low_candles, window_count)
    funding = funding_rates or []
    scores: list[CandidateScore] = []
    for spec in strategy_registry():
        for lookback in spec.parameter_grid:
            config = replace(base, entry_model=spec.strategy_id, entry_lookback=lookback)
            validation_results: list[BacktestResult] = []
            training_ok = True
            for train, validation in folds:
                train_result = _evaluate(train, high_candles, funding, config, market_rules)
                if train_result is None or train_result.max_drawdown > 0.5:
                    training_ok = False
                    break
                result = _evaluate(validation, high_candles, funding, config, market_rules)
                if result is not None:
                    validation_results.append(result)
            returns = [item.total_return for item in validation_results]
            drawdowns = [item.max_drawdown for item in validation_results]
            sharpes = [max(-3.0, min(3.0, item.sharpe)) for item in validation_results]
            trades = sum(len(item.trades) for item in validation_results)
            eligible = (
                training_ok
                and len(validation_results) == len(folds)
                and trades >= min_total_trades
                and max(drawdowns, default=1.0) <= max_allowed_drawdown
            )
            stability = pstdev(returns) if len(returns) > 1 else 1.0
            score = None
            if eligible:
                score = (
                    median(returns) - 1.5 * max(drawdowns) + 0.03 * fmean(sharpes) - 0.5 * stability
                )
            scores.append(
                CandidateScore(
                    spec.strategy_id,
                    spec.version,
                    lookback,
                    score,
                    tuple(returns),
                    median(returns) if returns else 0.0,
                    max(drawdowns) if drawdowns else 1.0,
                    median(sharpes) if sharpes else 0.0,
                    trades,
                    eligible,
                )
            )
    leaderboard = tuple(
        sorted(
            scores, key=lambda item: item.score if item.score is not None else -1e100, reverse=True
        )
    )
    selected = leaderboard[0] if leaderboard and leaderboard[0].eligible else None
    final_payload: dict[str, Any] | None = None
    stress_payload: dict[str, Any] | None = None
    if selected:
        selected_config = replace(
            base, entry_model=selected.strategy_id, entry_lookback=selected.lookback
        )
        final_result = _evaluate(final_test, high_candles, funding, selected_config, market_rules)
        if final_result:
            final_payload = {
                "return": final_result.total_return,
                "max_drawdown": final_result.max_drawdown,
                "sharpe": final_result.sharpe,
                "trades": len(final_result.trades),
                "profit_factor": final_result.profit_factor,
                "regimes": _regime_metrics(final_test, final_result),
            }
            stressed = replace(
                selected_config,
                fee_rate=min(0.019, selected_config.fee_rate * 2),
                slippage_rate=min(0.019, selected_config.slippage_rate * 2),
            )
            stressed_result = _evaluate(final_test, high_candles, funding, stressed, market_rules)
            stress_payload = {
                "double_cost_return": stressed_result.total_return if stressed_result else None,
                "monte_carlo_95_drawdown": _monte_carlo_drawdown(final_result),
                "passes_cost_stress": bool(stressed_result and stressed_result.total_return > 0),
            }
    return SelectionResult(
        base.symbol, selected, leaderboard, len(folds), final_payload, stress_payload
    )
