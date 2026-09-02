from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_quant.portfolio_accounting import (
    build_account_snapshot,
    initialize_account_transaction,
    withdrawal_transaction,
)
from crypto_quant.portfolio_contracts import (
    AccountBalances,
    AccountHaltReason,
    AccountPolicy,
    AccountSnapshot,
    AccountWatermarks,
    JournalAccount,
    JournalPosting,
    JournalSourceKind,
    JournalTransaction,
    PortfolioPosition,
    PositionSide,
    PostingSide,
)


def test_journal_requires_exactly_balanced_postings() -> None:
    with pytest.raises(ValueError, match="not balanced"):
        JournalTransaction(
            transaction_id="bad-journal",
            account_id="paper",
            source_kind=JournalSourceKind.DEPOSIT,
            source_id="deposit-1",
            event_time=1,
            asset="USDT",
            postings=(
                JournalPosting(
                    JournalAccount.CASH,
                    PostingSide.DEBIT,
                    Decimal("10"),
                ),
                JournalPosting(
                    JournalAccount.EXTERNAL_CAPITAL,
                    PostingSide.CREDIT,
                    Decimal("9"),
                ),
            ),
            evidence={},
        )


def test_initialization_is_deterministic_and_preserves_accounting_identity() -> None:
    policy = AccountPolicy()
    first = initialize_account_transaction(
        account_id="paper",
        initial_capital="1000",
        event_time=10,
        source_id="initial-capital-v1",
        policy=policy,
    )
    second = initialize_account_transaction(
        account_id="paper",
        initial_capital=Decimal("1000"),
        event_time=10,
        source_id="initial-capital-v1",
        policy=policy,
    )

    assert first == second
    assert first.debit_total == first.credit_total == Decimal("1000.00000000")
    assert JournalTransaction.from_dict(first.to_dict()) == first

    balances = AccountBalances().apply(first)
    assert balances.cash == Decimal("1000.00000000")
    assert balances.external_capital == Decimal("1000.00000000")
    assert balances.settled_assets == balances.accounting_equity


def test_negative_asset_balance_is_rejected_without_weakening_double_entry() -> None:
    policy = AccountPolicy()
    initialized = AccountBalances().apply(
        initialize_account_transaction(
            account_id="paper",
            initial_capital="100",
            event_time=1,
            source_id="initial",
            policy=policy,
        )
    )
    withdrawal = withdrawal_transaction(
        account_id="paper",
        amount="101",
        event_time=2,
        source_id="withdrawal-too-large",
        policy=policy,
    )

    with pytest.raises(ValueError, match="asset balance cannot be negative"):
        initialized.apply(withdrawal)


def test_position_stop_geometry_and_snapshot_round_trip() -> None:
    balances = AccountBalances(
        cash=Decimal("900"),
        position_margin=Decimal("100"),
        external_capital=Decimal("1000"),
    )
    position = PortfolioPosition(
        position_id="position-1",
        account_id="paper",
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        quantity=Decimal("2"),
        average_entry_price=Decimal("100"),
        mark_price=Decimal("105"),
        stop_price=Decimal("99"),
        position_margin=Decimal("100"),
        initial_risk=Decimal("2"),
        maintenance_margin_rate=Decimal("0.005"),
        correlation_group="default",
        open_command_id="command-1",
        open_risk_decision_id="risk-1",
        open_intent_id="intent-1",
        opened_at=10,
        updated_at=20,
    )
    snapshot = build_account_snapshot(
        account_id="paper",
        observed_at=20,
        balances=balances,
        positions=(position,),
        day_start_equity=Decimal("1000"),
        peak_equity=Decimal("1010"),
        consecutive_losses=0,
        watermarks=AccountWatermarks(1, 2, 3, 4),
        halt_reasons=(AccountHaltReason.STALE_MARK,),
    ).with_storage_sequence(7)

    restored = AccountSnapshot.from_dict(snapshot.to_dict())
    assert restored == snapshot
    assert restored.equity == Decimal("1010")
    assert restored.unrealized_pnl == Decimal("10")
    assert restored.maintenance_margin == Decimal("1.050")

    risk = restored.to_risk_snapshot()
    assert risk.sequence == 7
    assert risk.positions[0].stop_price == 99
    assert risk.positions[0].entry_price == 100

    invalid = snapshot.to_dict()
    invalid["equity"] = "999"
    with pytest.raises(ValueError, match="inconsistent equity"):
        AccountSnapshot.from_dict(invalid)


def test_snapshot_rejects_foreign_or_future_position() -> None:
    balances = AccountBalances(cash=Decimal("100"), external_capital=Decimal("100"))
    position = PortfolioPosition(
        position_id="position-1",
        account_id="other",
        symbol="BTC/USDT:USDT",
        side=PositionSide.SHORT,
        quantity=Decimal("1"),
        average_entry_price=Decimal("100"),
        mark_price=Decimal("99"),
        stop_price=Decimal("101"),
        position_margin=Decimal("0"),
        initial_risk=Decimal("1"),
        maintenance_margin_rate=Decimal("0.005"),
        correlation_group="default",
        open_command_id="command-1",
        open_risk_decision_id="risk-1",
        open_intent_id="intent-1",
        opened_at=10,
        updated_at=11,
    )

    with pytest.raises(ValueError, match="another account"):
        build_account_snapshot(
            account_id="paper",
            observed_at=11,
            balances=balances,
            positions=(position,),
            day_start_equity=100,
            peak_equity=100,
            consecutive_losses=0,
            watermarks=AccountWatermarks(),
        )
    with pytest.raises(ValueError, match="snapshot precedes"):
        build_account_snapshot(
            account_id="other",
            observed_at=10,
            balances=balances,
            positions=(position,),
            day_start_equity=100,
            peak_equity=100,
            consecutive_losses=0,
            watermarks=AccountWatermarks(),
        )

    with pytest.raises(ValueError, match="short position stop"):
        replace(position, stop_price=Decimal("98"))


def test_policy_rejects_integer_or_non_finite_precision() -> None:
    with pytest.raises(ValueError, match="fractional precision"):
        AccountPolicy(settlement_quantum=Decimal("1"))
    with pytest.raises(ValueError, match="finite"):
        AccountPolicy(fee_reserve_rate=Decimal("NaN"))
