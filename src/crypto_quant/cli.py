from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace

from .api import app
from .backtest import run_backtest, run_portfolio_backtest
from .config import StrategyConfig
from .data import CcxtDataSource, days_ago_ms, timeframe_ms
from .engine import default_market_rules
from .paper import advance_paper, paper_status
from .selection import select_strategy_for_symbol
from .shadow import shadow_report
from .storage import Repository
from .strategy import strategy_registry


def _fetch(config: StrategyConfig, days: int):
    source = CcxtDataSource(config.exchange, config.market_type)
    since = days_ago_ms(days)
    low_count = min(40_000, days * 86_400_000 // timeframe_ms(config.low_timeframe) + 20)
    high_count = min(10_000, (days + 60) * 86_400_000 // timeframe_ms(config.high_timeframe) + 20)
    low = source.fetch_candles(config.symbol, config.low_timeframe, since, max_candles=low_count)
    high = source.fetch_candles(
        config.symbol, config.high_timeframe, since - 60 * 86_400_000, max_candles=high_count
    )
    try:
        funding = source.fetch_funding_rates(config.symbol, since, 1000, 20_000)
    except Exception:
        funding = []
    try:
        marks = source.fetch_mark_candles(
            config.symbol, config.low_timeframe, since, max_candles=low_count
        )
    except Exception:
        marks = []
    try:
        rules = source.fetch_market_rules(config.symbol)
    except Exception:
        rules = default_market_rules(config)
    return low, high, funding, marks, rules


def _config(args: argparse.Namespace) -> StrategyConfig:
    return replace(
        StrategyConfig.from_env(),
        exchange="binanceusdm",
        symbol=args.symbol,
        max_leverage=args.max_leverage,
        entry_model=getattr(args, "entry_model", "chan_fractal"),
        entry_lookback=getattr(args, "entry_lookback", 20),
    )


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def command_backtest(args: argparse.Namespace) -> None:
    config = _config(args)
    low, high, funding, marks, rules = _fetch(config, args.days)
    _print(run_backtest(low, high, config, funding, marks, rules).to_dict())


def command_select(args: argparse.Namespace) -> None:
    config = _config(args)
    low, high, funding, _marks, rules = _fetch(config, args.days)
    _print(select_strategy_for_symbol(low, high, config, funding, market_rules=rules).to_dict())


def command_paper(args: argparse.Namespace) -> None:
    config = _config(args)
    repository = Repository(os.path.join(args.state_dir, "crypto_quant.db"))
    while True:
        low, high, funding, marks, rules = _fetch(config, args.days)
        state = repository.load_engine_state("paper", config.initial_cash)
        state = advance_paper(state, low, high, config, funding, marks, rules)
        repository.save_engine_state("paper", state, int(time.time() * 1000))
        payload = paper_status(state, {config.symbol: low[-1].close})
        if args.shadow:
            payload["shadow"] = shadow_report(state, {config.symbol: rules})
        _print(payload)
        if args.once:
            return
        time.sleep(args.poll_seconds)


def _whitelist() -> list[str]:
    return [
        value.strip()
        for value in os.getenv("CQ_SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT").split(",")
        if value.strip()
    ]


def command_portfolio_backtest(args: argparse.Namespace) -> None:
    base = StrategyConfig.from_env()
    configs: dict[str, StrategyConfig] = {}
    lows = {}
    highs = {}
    funding = {}
    marks = {}
    rules = {}
    for symbol in _whitelist():
        config = replace(base, symbol=symbol, max_leverage=args.max_leverage)
        low, high, symbol_funding, symbol_marks, symbol_rules = _fetch(config, args.days)
        configs[symbol] = config
        lows[symbol] = low
        highs[symbol] = high
        funding[symbol] = symbol_funding
        marks[symbol] = symbol_marks
        rules[symbol] = symbol_rules
    _print(run_portfolio_backtest(lows, highs, configs, funding, marks, rules).to_dict())


def command_portfolio_paper(args: argparse.Namespace) -> None:
    base = StrategyConfig.from_env()
    repository = Repository(os.path.join(args.state_dir, "crypto_quant.db"))
    while True:
        state = repository.load_engine_state("paper", base.initial_cash)
        prices: dict[str, float] = {}
        shadow_rules = {}
        for symbol in _whitelist():
            config = replace(base, symbol=symbol, max_leverage=args.max_leverage)
            low, high, funding, marks, rules = _fetch(config, args.days)
            state = advance_paper(state, low, high, config, funding, marks, rules)
            prices[symbol] = low[-1].close
            shadow_rules[symbol] = rules
        repository.save_engine_state("paper", state, int(time.time() * 1000))
        payload = paper_status(state, prices)
        if args.shadow:
            payload["shadow"] = shadow_report(state, shadow_rules)
        _print(payload)
        if args.once:
            return
        time.sleep(args.poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Binance perpetual research and paper trading")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--symbol", default="BTC/USDT:USDT")
    common.add_argument("--days", type=int, default=60)
    common.add_argument("--max-leverage", type=float, default=100)
    common.add_argument(
        "--entry-model",
        choices=[spec.strategy_id for spec in strategy_registry()],
        default="chan_fractal",
    )
    common.add_argument("--entry-lookback", type=int, default=20)

    backtest = sub.add_parser("backtest", parents=[common])
    backtest.set_defaults(handler=command_backtest)
    select = sub.add_parser("select", parents=[common])
    select.set_defaults(handler=command_select)
    paper = sub.add_parser("paper", parents=[common])
    paper.add_argument("--state-dir", default="data")
    paper.add_argument("--poll-seconds", type=int, default=60)
    paper.add_argument("--once", action="store_true")
    paper.add_argument("--shadow", action="store_true")
    paper.set_defaults(handler=command_paper)
    portfolio_backtest = sub.add_parser("portfolio-backtest")
    portfolio_backtest.add_argument("--days", type=int, default=180)
    portfolio_backtest.add_argument("--max-leverage", type=float, default=100)
    portfolio_backtest.set_defaults(handler=command_portfolio_backtest)
    portfolio_paper = sub.add_parser("portfolio-paper")
    portfolio_paper.add_argument("--days", type=int, default=60)
    portfolio_paper.add_argument("--max-leverage", type=float, default=100)
    portfolio_paper.add_argument("--state-dir", default="data")
    portfolio_paper.add_argument("--poll-seconds", type=int, default=60)
    portfolio_paper.add_argument("--once", action="store_true")
    portfolio_paper.add_argument("--shadow", action="store_true")
    portfolio_paper.set_defaults(handler=command_portfolio_paper)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    def serve_handler(args: argparse.Namespace) -> None:
        import uvicorn

        if (
            args.host not in {"127.0.0.1", "localhost", "::1"}
            and len(os.getenv("CQ_API_TOKEN", "")) < 32
        ):
            raise SystemExit(
                "CQ_API_TOKEN with at least 32 characters is required for network bind"
            )
        uvicorn.run(app, host=args.host, port=args.port)

    serve.set_defaults(handler=serve_handler)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
