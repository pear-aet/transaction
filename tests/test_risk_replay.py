from dataclasses import replace

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskDecisionFilter,
    RiskPolicy,
)
from crypto_quant.risk_replay import (
    DurableRiskFilter,
    RiskConsumerFilterMismatch,
    RiskReplayLedger,
)
from crypto_quant.risk_store import RiskDeliveryState, RiskStore
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
NOW = 900_001


def decision(index: int, *, mark_price: float = 100):
    strategy_intent = StrategyIntent(
        intent_id=f"intent-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=900_000,
        created_at=900_000,
        reference_price=100,
        stop_price=99,
        reason="replay_test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="hash",
        evidence={},
        storage_sequence=index,
    )
    account = AccountRiskSnapshot(
        f"account-{index}",
        "paper",
        900_000,
        10_000,
        10_000,
        0,
        10_000,
        10_000,
        0,
    )
    market_rules = MarketRules(
        SYMBOL,
        0.1,
        0.001,
        0.001,
        5,
        100,
        (RiskBracket(0, 1_000_000, 0.005),),
    )
    market = MarketRiskSnapshot(
        f"risk-market-{index}-{mark_price}",
        SYMBOL,
        900_000,
        mark_price,
        market_rules,
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


def seed(store: RiskStore) -> None:
    published = decision(1)
    recorded = decision(2, mark_price=110)
    expired = decision(3)
    store.commit_evaluation(published, updated_at=NOW)
    store.mark_published(published.decision_id, NOW)
    store.commit_evaluation(recorded, updated_at=NOW)
    store.commit_evaluation(expired, updated_at=NOW)
    store.mark_expired(expired.decision_id, expired.valid_until + 1)


def test_default_replay_exposes_only_published_execution_decisions(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    seed(store)
    ledger = RiskReplayLedger(store)

    batch = ledger.read_batch("execution-v1")
    assert batch.start_sequence == 0
    assert batch.high_watermark == 3
    assert batch.scanned_through == 3
    assert batch.skipped == 2
    assert [item.decision.intent_id for item in batch.decisions] == ["intent-1"]
    assert batch.decisions[0].delivery_state is RiskDeliveryState.PUBLISHED

    offset = ledger.commit_batch(batch, updated_at=NOW + 1)
    assert offset.sequence == 3
    assert ledger.read_batch("execution-v1").decisions == ()


def test_audit_filter_must_explicitly_request_nonpublished_states(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    seed(store)
    ledger = RiskReplayLedger(store)
    audit_filter = DurableRiskFilter(
        delivery_states=frozenset(
            {
                RiskDeliveryState.PUBLISHED,
                RiskDeliveryState.RECORDED,
                RiskDeliveryState.EXPIRED,
            }
        )
    )

    batch = ledger.read_batch("audit-v1", audit_filter)
    assert [item.delivery_state for item in batch.decisions] == [
        RiskDeliveryState.PUBLISHED,
        RiskDeliveryState.RECORDED,
        RiskDeliveryState.EXPIRED,
    ]


def test_risk_replay_filter_is_pinned_and_stale_batches_cannot_commit(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    seed(store)
    ledger = RiskReplayLedger(store)
    first = ledger.read_batch("execution-v1", limit=1)
    stale = ledger.read_batch("execution-v1", limit=3)
    ledger.commit_batch(first, updated_at=NOW + 1)

    with pytest.raises(ValueError, match="stale replay batch"):
        ledger.commit_batch(stale, updated_at=NOW + 2)
    with pytest.raises(RiskConsumerFilterMismatch, match="filter changed"):
        ledger.read_batch(
            "execution-v1",
            DurableRiskFilter(
                decision_filter=RiskDecisionFilter(symbols=frozenset({"ETH/USDT:USDT"}))
            ),
        )


def test_risk_replay_batch_cannot_commit_outside_snapshot(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    seed(store)
    ledger = RiskReplayLedger(store)
    batch = ledger.read_batch("execution-v1")

    with pytest.raises(ValueError, match="within its snapshot"):
        ledger.commit_batch(
            replace(batch, scanned_through=batch.high_watermark + 1),
            updated_at=NOW + 1,
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        ledger.commit_batch(
            replace(
                batch,
                high_watermark=batch.high_watermark + 1,
                scanned_through=batch.scanned_through + 1,
            ),
            updated_at=NOW + 1,
        )
