from decimal import Decimal

from crypto_quant.config import StrategyConfig
from crypto_quant.engine import EngineState, EventEngine, Position
from crypto_quant.execution_contracts import OrderLegType, OrderState
from crypto_quant.portfolio_contracts import AccountPolicy, JournalSourceKind
from crypto_quant.research_accounting import ResearchAccount

from portfolio_fixtures import NOW, SYMBOL, cumulative_update, execution_command, risk_decision


def test_research_account_uses_portfolio_primitives_and_matches_legacy_trade_math() -> None:
    policy = AccountPolicy()
    decision = risk_decision()
    command = execution_command(decision)
    quantity = Decimal(str(command.quantity))
    entry_price = Decimal("100")
    exit_price = Decimal("99")
    fee_rate = Decimal("0.001")
    funding_rate = Decimal("0.001")
    entry_quote = quantity * entry_price
    exit_quote = quantity * exit_price
    entry_fee = policy.quantize_money(entry_quote * fee_rate)
    exit_fee = policy.quantize_money(exit_quote * fee_rate)
    funding_payment = policy.quantize_money(quantity * entry_price * funding_rate)

    account = ResearchAccount.initialized(
        account_id="paper",
        initial_capital="10000",
        event_time=NOW - 1,
        source_id="research-parity-capital",
        policy=policy,
    )
    account.reserve(command=command, decision=decision, now_ms=NOW)
    primary = next(order for order in command.orders if order.leg_type is OrderLegType.PRIMARY)
    protective = next(
        order for order in command.orders if order.leg_type is OrderLegType.PROTECTIVE_STOP
    )
    opened = account.apply_open_update(
        command=command,
        decision=decision,
        order=primary,
        update=cumulative_update(
            primary,
            update_id="research-parity-open",
            cumulative_quantity=quantity,
            cumulative_quote_value=entry_quote,
            cumulative_fee=entry_fee,
            event_time=NOW + 1,
            state=OrderState.FILLED,
        ),
        maintenance_margin_rate=Decimal("0.005"),
    )
    funded = account.apply_funding(
        symbol=SYMBOL,
        rate=funding_rate,
        mark_price=entry_price,
        event_time=NOW + 2,
        source_id="research-parity-funding",
    )
    remaining = account.apply_close_update(
        command=command,
        order=protective,
        update=cumulative_update(
            protective,
            update_id="research-parity-close",
            cumulative_quantity=quantity,
            cumulative_quote_value=exit_quote,
            cumulative_fee=exit_fee,
            event_time=NOW + 3,
            state=OrderState.FILLED,
        ),
    )
    account.release(
        command_id=command.command_id,
        event_time=NOW + 4,
        reason="research_trade_complete",
    )

    assert opened.opening_fees == entry_fee
    assert funded.funding_pnl == -funding_payment
    assert remaining is None
    assert SYMBOL not in account.positions
    assert len(account.closed_positions) == 1
    closed = account.closed_positions[0]
    expected_gross = quantity * (exit_price - entry_price)
    expected_net = expected_gross - entry_fee - exit_fee - funding_payment
    assert closed.realized_pnl == expected_gross
    assert closed.opening_fees == entry_fee
    assert closed.closing_fees == exit_fee
    assert closed.funding_pnl == -funding_payment
    assert closed.net_pnl == expected_net
    assert account.balances.cash == Decimal("10000") + expected_net
    assert account.balances.settled_assets == Decimal("10000") + expected_net
    assert [transaction.source_kind for transaction in account.journal] == [
        JournalSourceKind.ACCOUNT_INITIALIZED,
        JournalSourceKind.COMMAND_RESERVED,
        JournalSourceKind.OPEN_FILL,
        JournalSourceKind.FUNDING,
        JournalSourceKind.CLOSE_FILL,
        JournalSourceKind.COMMAND_RELEASED,
    ]

    legacy_config = StrategyConfig(
        initial_cash=10_000,
        fee_rate=float(fee_rate),
        slippage_rate=0,
    )
    legacy_state = EngineState(cash=10_000 - float(entry_fee))
    legacy_state.positions[SYMBOL] = Position(
        symbol=SYMBOL,
        side="long",
        quantity=float(quantity),
        entry_price=float(entry_price),
        stop_price=float(decision.stop_price),
        take_profit=200,
        entry_time=NOW + 1,
        entry_fee=float(entry_fee),
        effective_leverage=float(decision.leverage),
        initial_risk=float(decision.initial_risk),
        maintenance_margin_rate=0.005,
    )
    legacy_state.funding_accrued[SYMBOL] = float(funding_payment)
    legacy_state.latest_prices[SYMBOL] = float(exit_price)
    legacy = EventEngine(legacy_config, state=legacy_state)
    legacy._close(SYMBOL, float(exit_price), NOW + 3, "research_parity")
    trade = legacy.state.trades[-1]

    assert policy.quantize_money(trade.gross_pnl) == closed.realized_pnl
    assert policy.quantize_money(trade.fees) == closed.opening_fees + closed.closing_fees
    assert policy.quantize_money(trade.funding) == funding_payment
    assert policy.quantize_money(trade.net_pnl) == closed.net_pnl
    assert policy.quantize_money(legacy.state.cash) == account.balances.cash
