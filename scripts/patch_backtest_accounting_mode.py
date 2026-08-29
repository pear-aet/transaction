from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 1:
        path.write_text(text.replace(old, new), encoding="utf-8")
        return
    if count == 0 and new in text:
        return
    raise SystemExit(f"expected one backtest accounting source or target, found {count}")


backtest = Path("src/crypto_quant/backtest.py")
replace_once(
    backtest,
    '''from .engine import EngineState, EventEngine, Trade, max_drawdown, safe_profit_factor
''',
    '''from .engine import EngineState, EventEngine, Trade, max_drawdown, safe_profit_factor
from .research_engine import ResearchEventEngine
''',
)
replace_once(
    backtest,
    '''class PortfolioBacktestResult:
    results: dict[str, BacktestResult]
    aggregate: BacktestResult


def run_backtest(
''',
    '''class PortfolioBacktestResult:
    results: dict[str, BacktestResult]
    aggregate: BacktestResult


def _event_engine(
    config: StrategyConfig,
    *,
    market_rules: dict[str, MarketRules] | None = None,
    decision_function=None,
    accounting_mode: str,
) -> EventEngine:
    if accounting_mode == "legacy":
        return EventEngine(
            config,
            market_rules=market_rules,
            decision_function=decision_function,
        )
    if accounting_mode == "portfolio":
        return ResearchEventEngine(
            config,
            market_rules=market_rules,
            decision_function=decision_function,
        )
    raise ValueError("accounting_mode must be 'legacy' or 'portfolio'")


def run_backtest(
''',
)
replace_once(
    backtest,
    '''    strategy_config: StrategyConfig | None = None,
) -> BacktestResult:
''',
    '''    strategy_config: StrategyConfig | None = None,
    accounting_mode: str = "legacy",
    decision_function=None,
) -> BacktestResult:
''',
)
replace_once(
    backtest,
    '''    engine = EventEngine(config, market_rules=market_rules)
''',
    '''    engine = _event_engine(
        config,
        market_rules=market_rules,
        decision_function=decision_function,
        accounting_mode=accounting_mode,
    )
''',
)
replace_once(
    backtest,
    '''    strategy_configs: dict[str, StrategyConfig] | None = None,
) -> PortfolioBacktestResult:
''',
    '''    strategy_configs: dict[str, StrategyConfig] | None = None,
    accounting_mode: str = "legacy",
    decision_function=None,
) -> PortfolioBacktestResult:
''',
)
replace_once(
    backtest,
    '''    engine = EventEngine(base_config, market_rules=rules_by_symbol)
''',
    '''    engine = _event_engine(
        base_config,
        market_rules=rules_by_symbol,
        decision_function=decision_function,
        accounting_mode=accounting_mode,
    )
''',
)

cli = Path("src/crypto_quant/cli.py")
replace_once(
    cli,
    '''        strategy_config,
    )
''',
    '''        strategy_config,
        accounting_mode=args.accounting_mode,
    )
''',
)
replace_once(
    cli,
    '''        rules,
        strategy_configs,
    )
''',
    '''        rules,
        strategy_configs,
        accounting_mode=args.accounting_mode,
    )
''',
)
replace_once(
    cli,
    '''    backtest.add_argument("--strategy", action="append", default=[])
    backtest.set_defaults(handler=command_backtest)
''',
    '''    backtest.add_argument("--strategy", action="append", default=[])
    backtest.add_argument(
        "--accounting-mode",
        choices=("legacy", "portfolio"),
        default="legacy",
    )
    backtest.set_defaults(handler=command_backtest)
''',
)
replace_once(
    cli,
    '''    portfolio.add_argument("--strategy", action="append", default=[])
    portfolio.set_defaults(handler=command_portfolio_backtest)
''',
    '''    portfolio.add_argument("--strategy", action="append", default=[])
    portfolio.add_argument(
        "--accounting-mode",
        choices=("legacy", "portfolio"),
        default="legacy",
    )
    portfolio.set_defaults(handler=command_portfolio_backtest)
''',
)
