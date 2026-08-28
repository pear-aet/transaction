import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskPolicy,
)
from crypto_quant.risk_replay import DurableRiskFilter, RiskReplayLedger
from crypto_quant.risk_store import RiskDeliveryState, RiskStore
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
NOW = 1_000_000


def rules() -> MarketRules:
    return MarketRules(
        SYMBOL,
        tick_size=0.1,
        step_size=0.001,
        min_quantity=0.001,
        min_notional=5,
        max_leverage=100,
        brackets=(RiskBracket(0, 1_000_000, 0.005),),
    )


def intent(index: int, decision_time: int) -> StrategyIntent:
    return StrategyIntent(
        intent_id=f"intent-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=decision_time,
        created_at=decision_time,
        reference_price=100,
        stop_price=99,
        reason="risk_pending_barrier",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=index,
    )


def decision(index: int, *, mark_price: float):
    strategy_intent = intent(index, 900_000 + index)
    account = AccountRiskSnapshot(
        f"account-{index}",
        "paper",
        NOW,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    market = MarketRiskSnapshot(
        f"market-{index}-{mark_price}",
        SYMBOL,
        NOW,
        mark_price,
        rules(),
    )
    return evaluate_risk(
        RiskContext(
            "risk-primary",
            strategy_intent,
            account,
            market,
            RiskPolicy(),
            NOW,
        )
    )


def test_pending_decision_blocks_cursor_from_skipping_later_terminal_rows(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    pending = decision(1, mark_price=100)
    recorded = decision(2, mark_price=110)
    store.commit_evaluation(pending, updated_at=NOW)
    store.commit_evaluation(recorded, updated_at=NOW)
    ledger = RiskReplayLedger(store)

    blocked = ledger.read_batch("execution-v1")
    assert blocked.high_watermark == 2
    assert blocked.scanned_through == 0
    assert blocked.decisions == ()
    assert blocked.skipped == 0

    store.mark_published(pending.decision_id, NOW)
    released = ledger.read_batch("execution-v1")
    assert released.scanned_through == 2
    assert [item.decision.decision_id for item in released.decisions] == [
        pending.decision_id
    ]
    assert released.skipped == 1

    offset = ledger.commit_batch(released, updated_at=NOW + 1)
    assert offset.sequence == 2


def test_pending_risk_state_cannot_be_selected_for_durable_replay() -> None:
    with pytest.raises(ValueError, match="mutable pending"):
        DurableRiskFilter(delivery_states=frozenset({RiskDeliveryState.PENDING}))
