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
    '''from .strategy import decide
''',
    '''from .research_engine import ResearchEventEngine
from .strategy import decide
''',
)
replace_once(
    backtest,
    '''def _result(
    state: EngineState,
''',
    '''def _event_engine(
    config: StrategyConfig,
    *,
    market_rules: dict[str, MarketRules] | None,
    decision_function,
    accounting_mode: str,
) -> EventEngine:
    selected_decision = decide if decision_function is None else decision_function
    if accounting_mode == "legacy":
        return EventEngine(
            config,
            market_rules=market_rules,
            decision_function=selected_decision,
        )
    if accounting_mode == "portfolio":
        return ResearchEventEngine(
            config,
            market_rules=market_rules,
            decision_function=selected_decision,
        )
    raise ValueError("accounting_mode must be 'legacy' or 'portfolio'")


def _result(
    state: EngineState,
''',
)
replace_once(
    backtest,
    '''    mark_candles: list[Candle] | None = None,
    market_rules: MarketRules | None = None,
) -> BacktestResult:
''',
    '''    mark_candles: list[Candle] | None = None,
    market_rules: MarketRules | None = None,
    *,
    accounting_mode: str = "legacy",
    decision_function=None,
) -> BacktestResult:
''',
)
replace_once(
    backtest,
    '''    engine = EventEngine(config, market_rules=rules, decision_function=decide)
''',
    '''    engine = _event_engine(
        config,
        market_rules=rules,
        decision_function=decision_function,
        accounting_mode=accounting_mode,
    )
''',
)
replace_once(
    backtest,
    '''    marks_by_symbol: dict[str, list[Candle]] | None = None,
    rules_by_symbol: dict[str, MarketRules] | None = None,
) -> BacktestResult:
''',
    '''    marks_by_symbol: dict[str, list[Candle]] | None = None,
    rules_by_symbol: dict[str, MarketRules] | None = None,
    *,
    accounting_mode: str = "legacy",
    decision_function=None,
) -> BacktestResult:
''',
)
replace_once(
    backtest,
    '''    engine = EventEngine(base, market_rules=rules_by_symbol, decision_function=decide)
''',
    '''    engine = _event_engine(
        base,
        market_rules=rules_by_symbol,
        decision_function=decision_function,
        accounting_mode=accounting_mode,
    )
''',
)

cli = Path("src/crypto_quant/cli.py")
replace_once(
    cli,
    '''    _print(run_backtest(low, high, config, funding, marks, rules).to_dict())
''',
    '''    _print(
        run_backtest(
            low,
            high,
            config,
            funding,
            marks,
            rules,
            accounting_mode=args.accounting_mode,
        ).to_dict()
    )
''',
)
replace_once(
    cli,
    '''    _print(run_portfolio_backtest(lows, highs, configs, funding, marks, rules).to_dict())
''',
    '''    _print(
        run_portfolio_backtest(
            lows,
            highs,
            configs,
            funding,
            marks,
            rules,
            accounting_mode=args.accounting_mode,
        ).to_dict()
    )
''',
)
replace_once(
    cli,
    '''    backtest = sub.add_parser("backtest", parents=[common])
    backtest.set_defaults(handler=command_backtest)
''',
    '''    backtest = sub.add_parser("backtest", parents=[common])
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
    '''    portfolio_backtest.add_argument("--max-leverage", type=float, default=100)
    portfolio_backtest.set_defaults(handler=command_portfolio_backtest)
''',
    '''    portfolio_backtest.add_argument("--max-leverage", type=float, default=100)
    portfolio_backtest.add_argument(
        "--accounting-mode",
        choices=("legacy", "portfolio"),
        default="legacy",
    )
    portfolio_backtest.set_defaults(handler=command_portfolio_backtest)
''',
)
