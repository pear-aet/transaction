import math

import pytest

from crypto_quant.config import StrategyConfig
from crypto_quant.domain import Candle, FundingRate, Signal
from crypto_quant.engine import EngineState, EventEngine
from crypto_quant.portfolio_contracts import JournalSourceKind
from crypto_quant.research_engine import ResearchEventEngine

from portfolio_fixtures import NOW, SYMBOL, market_rules


def config() -> StrategyConfig:
    return StrategyConfig(
        symbol=SYMBOL,
        initial_cash=10_000,
        fee_rate=0.001,
        slippage_rate=0.0005,
        liquidation_fee_rate=0.005,
        fallback_funding_rate_8h=0.0001,
    )


def assert_close(left: float, right: float, tolerance: float = 1e-7) -> None:
    assert math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


def open_pair(side: str = "long"):
    selected = config()
    rules = {SYMBOL: market_rules()}
    legacy = EventEngine(selected, market_rules=rules.copy())
    research = ResearchEventEngine(selected, market_rules=rules.copy())
    signal = Signal(
        timestamp=NOW,
        action=f"enter_{side}",
        reason=f"research_{side}_parity",
        stop_price=99 if side == "long" else 101,
    )
    bar = Candle(NOW, 100, 100, 100, 100)
    legacy._open(SYMBOL, signal, bar)
    research._open(SYMBOL, signal, bar)
    return legacy, research, bar


def test_long_trade_funding_and_explicit_exit_match_legacy_engine() -> None:
    legacy, research, bar = open_pair("long")
    legacy_position = legacy.state.positions[SYMBOL]
    research_position = research.state.positions[SYMBOL]
    assert_close(research_position.quantity, legacy_position.quantity)
    assert_close(research_position.entry_price, legacy_position.entry_price)
    assert_close(research_position.stop_price, legacy_position.stop_price)
    assert_close(research_position.take_profit, legacy_position.take_profit)
    assert_close(research_position.entry_fee, legacy_position.entry_fee)
    assert_close(research.state.cash, legacy.state.cash)
    assert_close(research.equity(), legacy.equity())

    rate = FundingRate(NOW + 1, 0.001)
    legacy._apply_funding(SYMBOL, bar, [rate])
    research._apply_funding(SYMBOL, bar, [rate])
    assert_close(
        research.state.funding_accrued[SYMBOL],
        legacy.state.funding_accrued[SYMBOL],
    )
    assert_close(research.state.cash, legacy.state.cash)
    assert_close(research.equity(), legacy.equity())

    legacy._close(SYMBOL, 101, NOW + 2, "strategy_exit")
    research._close(SYMBOL, 101, NOW + 2, "strategy_exit")

    legacy_trade = legacy.state.trades[-1]
    research_trade = research.state.trades[-1]
    assert_close(research_trade.entry_price, legacy_trade.entry_price)
    assert_close(research_trade.exit_price, legacy_trade.exit_price)
    assert_close(research_trade.quantity, legacy_trade.quantity)
    assert_close(research_trade.gross_pnl, legacy_trade.gross_pnl)
    assert_close(research_trade.fees, legacy_trade.fees)
    assert_close(research_trade.funding, legacy_trade.funding)
    assert_close(research_trade.net_pnl, legacy_trade.net_pnl)
    assert_close(research.state.cash, legacy.state.cash)
    assert research.state.positions == {}
    assert research.account.positions == {}
    assert len(research.account.closed_positions) == 1


def test_short_trade_matches_legacy_engine_signs() -> None:
    legacy, research, bar = open_pair("short")
    rate = FundingRate(NOW + 1, 0.001)
    legacy._apply_funding(SYMBOL, bar, [rate])
    research._apply_funding(SYMBOL, bar, [rate])
    assert legacy.state.funding_accrued[SYMBOL] < 0
    assert research.state.funding_accrued[SYMBOL] < 0
    assert_close(research.equity(), legacy.equity())

    legacy._close(SYMBOL, 99, NOW + 2, "strategy_exit")
    research._close(SYMBOL, 99, NOW + 2, "strategy_exit")
    legacy_trade = legacy.state.trades[-1]
    research_trade = research.state.trades[-1]
    assert_close(research_trade.gross_pnl, legacy_trade.gross_pnl)
    assert_close(research_trade.funding, legacy_trade.funding)
    assert_close(research_trade.net_pnl, legacy_trade.net_pnl)
    assert_close(research.state.cash, legacy.state.cash)


def test_stop_loss_uses_original_entry_command_and_releases_reservation() -> None:
    _, research, _ = open_pair("long")
    entry_plan = research.trading.entry_plans[SYMBOL]
    assert entry_plan.command is not None

    research._close(SYMBOL, 99, NOW + 2, "stop_loss")

    closed = research.account.closed_positions[-1]
    assert closed.open_command_id == entry_plan.command.command_id
    assert closed.close_command_id == entry_plan.command.command_id
    reservation = research.account.reservations[entry_plan.command.command_id]
    assert reservation.state.value == "released"
    assert [transaction.source_kind for transaction in research.account.journal] == [
        JournalSourceKind.ACCOUNT_INITIALIZED,
        JournalSourceKind.COMMAND_RESERVED,
        JournalSourceKind.OPEN_FILL,
        JournalSourceKind.CLOSE_FILL,
        JournalSourceKind.COMMAND_RELEASED,
    ]


def test_process_bar_marks_position_once_and_duplicate_bar_is_ignored() -> None:
    _, research, _ = open_pair("long")
    before_version = research.account.positions[SYMBOL].version
    next_bar = Candle(NOW + 900_000, 100, 102, 100, 101)

    research.process_bar(
        SYMBOL,
        next_bar,
        [next_bar],
        [next_bar],
        generate_signal=False,
    )
    after = research.account.positions[SYMBOL]
    assert after.mark_price == 101
    assert after.version == before_version + 1
    assert_close(research.equity(), research.state.cash + research_position_pnl(research))

    research.process_bar(
        SYMBOL,
        next_bar,
        [next_bar],
        [next_bar],
        generate_signal=False,
    )
    assert research.account.positions[SYMBOL].version == after.version


def research_position_pnl(engine: ResearchEventEngine) -> float:
    position = engine.state.positions[SYMBOL]
    price = engine.state.latest_prices[SYMBOL]
    sign = 1 if position.side == "long" else -1
    return sign * position.quantity * (price - position.entry_price) - engine.state.funding_accrued.get(
        SYMBOL,
        0.0,
    )


def test_nonempty_legacy_state_is_rejected_instead_of_guessed() -> None:
    state = EngineState(10_000)
    state.events.append({"kind": "legacy"})

    with pytest.raises(ValueError, match="fresh state only"):
        ResearchEventEngine(config(), state=state)
