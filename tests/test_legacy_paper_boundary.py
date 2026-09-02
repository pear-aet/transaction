import argparse

import pytest

from crypto_quant.cli import build_parser, command_paper, command_portfolio_paper
from crypto_quant.config import StrategyConfig
from crypto_quant.engine import EngineState
from crypto_quant.paper import LEGACY_PAPER_DISABLED, advance_paper, paper_status


def test_direct_legacy_paper_advance_requires_explicit_opt_in() -> None:
    state = EngineState(1000)
    config = StrategyConfig(initial_cash=1000)

    with pytest.raises(RuntimeError, match="disabled by default"):
        advance_paper(state, [], [], config)

    assert (
        advance_paper(
            state,
            [],
            [],
            config,
            allow_legacy_event_engine=True,
        )
        is state
    )


def test_cli_paper_handlers_fail_before_network_or_state_access() -> None:
    args = argparse.Namespace(legacy_event_engine=False)

    with pytest.raises(SystemExit, match="PortfolioRuntime"):
        command_paper(args)
    with pytest.raises(SystemExit, match="PortfolioRuntime"):
        command_portfolio_paper(args)


def test_cli_requires_visible_legacy_flag_for_old_paper_commands() -> None:
    parser = build_parser()

    paper = parser.parse_args(["paper", "--once"])
    portfolio = parser.parse_args(["portfolio-paper", "--once"])
    enabled = parser.parse_args(["paper", "--once", "--legacy-event-engine"])

    assert paper.legacy_event_engine is False
    assert portfolio.legacy_event_engine is False
    assert enabled.legacy_event_engine is True


def test_legacy_status_is_never_presented_as_authoritative() -> None:
    status = paper_status(EngineState(1000), {})

    assert status["account_source"] == "legacy_event_engine"
    assert status["authoritative"] is False
    assert LEGACY_PAPER_DISABLED.startswith("legacy EventEngine")
    assert any("research-only" in warning for warning in status["warnings"])
