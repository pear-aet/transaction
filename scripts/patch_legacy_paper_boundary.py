from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one legacy paper match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


PAPER = "src/crypto_quant/paper.py"
CLI = "src/crypto_quant/cli.py"

replace_once(
    PAPER,
    '''PaperState = EngineState
''',
    '''PaperState = EngineState
LEGACY_PAPER_DISABLED = (
    "legacy EventEngine paper accounting is disabled by default; "
    "authoritative paper accounts must use PortfolioRuntime and PortfolioStore"
)
''',
)
replace_once(
    PAPER,
    '''    market_rules: MarketRules | None = None,
) -> PaperState:
    """Advance persistent paper state using the shared backtest event engine."""
    if not low_candles:
''',
    '''    market_rules: MarketRules | None = None,
    *,
    allow_legacy_event_engine: bool = False,
) -> PaperState:
    """Advance the explicitly enabled research-only legacy paper state."""
    if not allow_legacy_event_engine:
        raise RuntimeError(LEGACY_PAPER_DISABLED)
    if not low_candles:
''',
)
replace_once(
    PAPER,
    '''    return {
        "cash": state.cash,
''',
    '''    return {
        "account_source": "legacy_event_engine",
        "authoritative": False,
        "cash": state.cash,
''',
)
replace_once(
    PAPER,
    '''        "warnings": sorted(state.warnings),
    }
''',
    '''        "warnings": sorted(
            state.warnings
            | {
                "Legacy EventEngine paper state is research-only and is not an "
                "authoritative Portfolio account."
            }
        ),
    }
''',
)

replace_once(
    CLI,
    '''from .paper import advance_paper, paper_status
''',
    '''from .paper import LEGACY_PAPER_DISABLED, advance_paper, paper_status
from .portfolio_store import PortfolioStore
''',
)
replace_once(
    CLI,
    '''def command_paper(args: argparse.Namespace) -> None:
    config = _config(args)
''',
    '''def command_paper(args: argparse.Namespace) -> None:
    if not args.legacy_event_engine:
        raise SystemExit(LEGACY_PAPER_DISABLED)
    config = _config(args)
''',
)
replace_once(
    CLI,
    '''        state = advance_paper(state, low, high, config, funding, marks, rules)
''',
    '''        state = advance_paper(
            state,
            low,
            high,
            config,
            funding,
            marks,
            rules,
            allow_legacy_event_engine=True,
        )
''',
)
replace_once(
    CLI,
    '''def command_portfolio_paper(args: argparse.Namespace) -> None:
    base = StrategyConfig.from_env()
''',
    '''def command_portfolio_paper(args: argparse.Namespace) -> None:
    if not args.legacy_event_engine:
        raise SystemExit(LEGACY_PAPER_DISABLED)
    base = StrategyConfig.from_env()
''',
)
replace_once(
    CLI,
    '''            state = advance_paper(state, low, high, config, funding, marks, rules)
''',
    '''            state = advance_paper(
                state,
                low,
                high,
                config,
                funding,
                marks,
                rules,
                allow_legacy_event_engine=True,
            )
''',
)
replace_once(
    CLI,
    '''def build_parser() -> argparse.ArgumentParser:
''',
    '''def command_portfolio_status(args: argparse.Namespace) -> None:
    store = PortfolioStore(os.path.join(args.state_dir, "crypto_quant.db"))
    snapshot = store.latest_snapshot(args.account_id)
    if snapshot is None:
        raise SystemExit(f"authoritative account snapshot is unavailable: {args.account_id}")
    _print(snapshot.to_dict())


def build_parser() -> argparse.ArgumentParser:
''',
)
replace_once(
    CLI,
    '''    paper.add_argument("--shadow", action="store_true")
    paper.set_defaults(handler=command_paper)
''',
    '''    paper.add_argument("--shadow", action="store_true")
    paper.add_argument(
        "--legacy-event-engine",
        action="store_true",
        help="explicitly enable the non-authoritative legacy research paper account",
    )
    paper.set_defaults(handler=command_paper)
''',
)
replace_once(
    CLI,
    '''    portfolio_paper.add_argument("--shadow", action="store_true")
    portfolio_paper.set_defaults(handler=command_portfolio_paper)
    serve = sub.add_parser("serve")
''',
    '''    portfolio_paper.add_argument("--shadow", action="store_true")
    portfolio_paper.add_argument(
        "--legacy-event-engine",
        action="store_true",
        help="explicitly enable the non-authoritative legacy research paper account",
    )
    portfolio_paper.set_defaults(handler=command_portfolio_paper)
    portfolio_status = sub.add_parser("portfolio-status")
    portfolio_status.add_argument("--state-dir", default="data")
    portfolio_status.add_argument("--account-id", default="paper")
    portfolio_status.set_defaults(handler=command_portfolio_status)
    serve = sub.add_parser("serve")
''',
)
