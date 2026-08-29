import inspect

from crypto_quant.backtest import run_backtest, run_portfolio_backtest
from crypto_quant.cli import build_parser


def test_backtests_default_to_authoritative_portfolio_accounting() -> None:
    assert inspect.signature(run_backtest).parameters["accounting_mode"].default == "portfolio"
    assert (
        inspect.signature(run_portfolio_backtest).parameters["accounting_mode"].default
        == "portfolio"
    )


def test_cli_requires_explicit_choice_to_use_legacy_accounting() -> None:
    parser = build_parser()
    single = parser.parse_args(["backtest"])
    portfolio = parser.parse_args(["portfolio-backtest"])
    legacy = parser.parse_args(["backtest", "--accounting-mode", "legacy"])

    assert single.accounting_mode == "portfolio"
    assert portfolio.accounting_mode == "portfolio"
    assert legacy.accounting_mode == "legacy"
