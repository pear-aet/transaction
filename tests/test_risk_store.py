from dataclasses import replace

import pytest

from crypto_quant.domain import MarketRules, RiskBracket
from crypto_quant.risk import evaluate_risk
from crypto_quant.risk_contracts import (
    AccountRiskSnapshot,
    MarketRiskSnapshot,
    RiskContext,
    RiskDecisionStatus,
    RiskPolicy,
)
from crypto_quant.risk_store import (
    RiskDecisionConflict,
    RiskDeliveryState,
    RiskInstanceError,
    RiskStore,
)
from crypto_quant.strategy_contracts import StrategyAction, StrategyIntent

SYMBOL = "BTC/USDT:USDT"
NOW = 900_001


def rules() -> MarketRules:
    return MarketRules(
        SYMBOL,
        0.1,
        0.001,
        0.001,
        5,
        100,
        (RiskBracket(0, 1_000_000, 0.005),),
    )


def intent(index: int) -> StrategyIntent:
    return StrategyIntent(
        intent_id=f"intent-{index}",
        strategy_id="test",
        strategy_version="1.0.0",
        instance_id="strategy-primary",
        symbol=SYMBOL,
        action=StrategyAction.ENTER_LONG,
        decision_time=900_000 + index,
        created_at=900_000 + index,
        reference_price=100,
        stop_price=99,
        reason="store_test",
        source_event_id=f"market-{index}",
        source_sequence=index,
        high_timeframe="1h",
        low_timeframe="15m",
        parameter_hash="strategy-hash",
        evidence={},
        storage_sequence=index,
    )


def decision(
    index: int,
    *,
    engine_instance_id: str = "risk-primary",
    market_price: float = 100,
    policy: RiskPolicy | None = None,
):
    selected_policy = policy or RiskPolicy()
    strategy_intent = intent(index)
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
        f"market-{index}-{market_price}",
        SYMBOL,
        NOW,
        market_price,
        rules(),
    )
    return evaluate_risk(
        RiskContext(
            engine_instance_id,
            strategy_intent,
            account,
            market,
            selected_policy,
            NOW,
        )
    )


def test_approved_decision_and_evaluation_commit_atomically(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    value = decision(1)
    commit = store.commit_evaluation(value, updated_at=NOW)

    assert value.status is RiskDecisionStatus.APPROVED
    assert commit.duplicate is False
    assert commit.decision_inserted is True
    assert commit.stored_decision.delivery_state is RiskDeliveryState.PENDING
    assert commit.stored_decision.decision.storage_sequence == 1
    assert store.load_evaluation("risk-primary", value.intent_id).decision_id == value.decision_id
    assert store.metrics() == {
        "instances": 1,
        "evaluations": 1,
        "decisions": 1,
        "pending_decisions": 1,
        "published_decisions": 0,
        "recorded_decisions": 0,
        "expired_decisions": 0,
        "latest_decision_sequence": 1,
    }

    duplicate = store.commit_evaluation(value, updated_at=NOW + 1)
    assert duplicate.duplicate is True
    assert duplicate.decision_inserted is False
    assert duplicate.stored_decision.decision.storage_sequence == 1


def test_publication_and_expiry_are_monotonic_and_validity_bounded(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    publishable = decision(1)
    store.commit_evaluation(publishable, updated_at=NOW)
    assert store.mark_published(publishable.decision_id, publishable.valid_until) is True
    assert store.mark_published(publishable.decision_id, publishable.valid_until) is False
    with pytest.raises(ValueError, match="cannot expire"):
        store.mark_expired(publishable.decision_id, publishable.valid_until + 1)

    expiring = decision(2)
    store.commit_evaluation(expiring, updated_at=NOW)
    with pytest.raises(ValueError, match="before validity"):
        store.mark_expired(expiring.decision_id, expiring.valid_until)
    assert store.mark_expired(expiring.decision_id, expiring.valid_until + 1) is True
    assert store.mark_expired(expiring.decision_id, expiring.valid_until + 2) is False
    with pytest.raises(ValueError, match="cannot be published"):
        store.mark_published(expiring.decision_id, expiring.valid_until + 2)


def test_rejected_decision_is_recorded_without_execution_delivery(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    rejected = decision(1, market_price=110)
    commit = store.commit_evaluation(rejected, updated_at=NOW)

    assert rejected.status is RiskDecisionStatus.REJECTED
    assert commit.stored_decision.delivery_state is RiskDeliveryState.RECORDED
    assert store.pending_decisions() == []
    with pytest.raises(ValueError, match="cannot be published"):
        store.mark_published(rejected.decision_id, NOW)
    with pytest.raises(ValueError, match="cannot expire"):
        store.mark_expired(rejected.decision_id, NOW + 1)


def test_one_intent_cannot_be_redecided_with_new_snapshots(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    first = decision(1)
    changed = decision(1, market_price=100.5)
    store.commit_evaluation(first, updated_at=NOW)

    assert first.decision_id != changed.decision_id
    with pytest.raises(RiskDecisionConflict, match="changed for intent"):
        store.commit_evaluation(changed, updated_at=NOW + 1)
    assert store.metrics()["decisions"] == 1
    assert store.metrics()["evaluations"] == 1


def test_risk_instance_pins_account_and_policy_identity(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    first = decision(1)
    changed_policy = replace(RiskPolicy(), risk_per_trade=0.005)
    second = decision(2, policy=changed_policy)
    store.commit_evaluation(first, updated_at=NOW)

    with pytest.raises(RiskInstanceError, match="use a new engine_instance_id"):
        store.commit_evaluation(second, updated_at=NOW + 1)
    assert store.metrics()["decisions"] == 1


def test_static_decision_queries_filter_all_dimensions(tmp_path) -> None:
    store = RiskStore(tmp_path / "quant.db")
    primary = decision(1)
    secondary = decision(2, engine_instance_id="risk-secondary")
    store.commit_evaluation(primary, updated_at=NOW)
    store.commit_evaluation(secondary, updated_at=NOW)

    rows = store.load_decisions(
        after_sequence=0,
        through_sequence=1,
        engine_instance_id="risk-primary",
        account_id="paper",
        symbol=SYMBOL,
    )
    assert [item.decision.decision_id for item in rows] == [primary.decision_id]
    assert store.load_decisions(after_sequence=2, through_sequence=1) == []
    listed = store.list_decisions(engine_instance_id="risk-secondary", symbol=SYMBOL)
    assert [item.decision.decision_id for item in listed] == [secondary.decision_id]
