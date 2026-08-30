from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 1:
        file.write_text(text.replace(old, new), encoding="utf-8")
        return
    if count == 0 and new in text:
        return
    raise SystemExit(
        f"expected one finalization source or existing target in {path}, found {count}"
    )


def replace_all(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count > 0:
        file.write_text(text.replace(old, new), encoding="utf-8")
        return
    if new in text:
        return
    raise SystemExit(f"finalization anchor is missing in {path}")


def append_once(path: str, marker: str, content: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if marker in text:
        return
    file.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


RUNTIME = "src/crypto_quant/portfolio_runtime.py"
STORE = "src/crypto_quant/portfolio_store.py"
ENGINE = "src/crypto_quant/portfolio_engine.py"
ACCOUNTING = "src/crypto_quant/portfolio_accounting.py"
ENGINE_TEST = "tests/test_portfolio_engine.py"
STORE_TEST = "tests/test_portfolio_store.py"
RUNTIME_TEST = "tests/test_portfolio_runtime.py"
LIFECYCLE_TEST = "tests/test_portfolio_lifecycle.py"

replace_once(
    STORE,
    '''import threading
from dataclasses import dataclass
''',
    '''import threading
from dataclasses import dataclass
from decimal import Decimal
''',
)

replace_all(
    STORE,
    '"UPDATE account_instances SET updated_at=? WHERE account_id=?"',
    '"UPDATE account_instances SET updated_at=max(updated_at,?) WHERE account_id=?"',
)

replace_once(
    STORE,
    '''                return SnapshotCommit(existing, True)
            positions = self._positions_for_account(connection, snapshot.account_id)
''',
    '''                return SnapshotCommit(existing, True)
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (snapshot.account_id,),
            ).fetchone()
            if instance_row is None:
                raise KeyError(f"account not found: {snapshot.account_id}")
            instance = self._instance_from_row(instance_row)
            if snapshot.observed_at < instance.updated_at:
                raise AccountStoreConflict("snapshot time precedes account state")
            latest_row = connection.execute(
                """SELECT * FROM account_snapshots
                WHERE account_id=? ORDER BY observed_at DESC,storage_sequence DESC
                LIMIT 1""",
                (snapshot.account_id,),
            ).fetchone()
            if latest_row is not None:
                latest = AccountSnapshot.from_dict(
                    json.loads(str(latest_row["payload"]))
                ).with_storage_sequence(int(latest_row["storage_sequence"]))
                if snapshot.observed_at < latest.observed_at:
                    raise AccountStoreConflict(
                        "snapshot time moved behind the latest snapshot"
                    )
                if snapshot.observed_at == latest.observed_at:
                    raise AccountStoreConflict(
                        "same-time snapshot changed immutable account semantics"
                    )
            positions = self._positions_for_account(connection, snapshot.account_id)
''',
)

replace_once(
    ACCOUNTING,
    '''    initial_risk_after = max(ZERO, approved_risk_after + price_risk_adjustment)
    initial_risk_before = ZERO if position is None else position.initial_risk
    initial_risk_delta = initial_risk_after - initial_risk_before
    risk_overrun = max(ZERO, initial_risk_after - approved_risk_after)
''',
    '''    initial_risk_after = max(ZERO, approved_risk_after + price_risk_adjustment)
    risk_overrun = max(ZERO, initial_risk_after - approved_risk_after)
''',
)

replace_once(
    ENGINE,
    '''        if current > wall_now:
            raise PortfolioHealthError("portfolio snapshot time cannot be in the future")
        instance = self.store.load_instance(self.settings.account_id)
        if instance is None:
            raise PortfolioHealthError("portfolio account has not been initialized")
        positions = tuple(self.store.list_positions(self.settings.account_id))
''',
    '''        if current > wall_now:
            raise PortfolioHealthError("portfolio snapshot time cannot be in the future")
        instance = self.store.load_instance(self.settings.account_id)
        if instance is None:
            raise PortfolioHealthError("portfolio account has not been initialized")
        if current < instance.updated_at:
            raise PortfolioHealthError("portfolio snapshot time precedes account state")
        positions = tuple(self.store.list_positions(self.settings.account_id))
''',
)

replace_once(
    ENGINE,
    '''        prior = self.store.latest_snapshot(self.settings.account_id)
        balances = self.store.balances(self.settings.account_id)
''',
    '''        prior = self.store.latest_snapshot(self.settings.account_id)
        if prior is not None and current < prior.observed_at:
            raise PortfolioHealthError("portfolio snapshot time moved backwards")
        balances = self.store.balances(self.settings.account_id)
''',
)

replace_once(
    ENGINE,
    '''            adjusted_day_start = (
                prior.day_start_equity + external_capital_delta
                if self._utc_day(prior.observed_at) == self._utc_day(current)
                else prior.equity + external_capital_delta
            )
            adjusted_peak = prior.peak_equity + external_capital_delta
            if adjusted_day_start < ZERO or adjusted_peak < ZERO:
                raise PortfolioHealthError(
                    "external cash flow made Portfolio equity anchors negative"
                )
''',
    '''            raw_day_start = (
                prior.day_start_equity + external_capital_delta
                if self._utc_day(prior.observed_at) == self._utc_day(current)
                else prior.equity + external_capital_delta
            )
            adjusted_day_start = max(ZERO, raw_day_start)
            adjusted_peak = max(ZERO, prior.peak_equity + external_capital_delta)
''',
)

replace_once(
    RUNTIME,
    '''    reservations_released: int = 0
    snapshots_created: int = 0
    failures: int = 0
''',
    '''    reservations_released: int = 0
    reconciliation_findings: int = 0
    audit_findings: int = 0
    snapshots_created: int = 0
    failures: int = 0
''',
)
replace_once(
    RUNTIME,
    '''            "reservations_released": self.reservations_released,
            "snapshots_created": self.snapshots_created,
''',
    '''            "reservations_released": self.reservations_released,
            "reconciliation_findings": self.reconciliation_findings,
            "audit_findings": self.audit_findings,
            "snapshots_created": self.snapshots_created,
''',
)
replace_once(
    RUNTIME,
    '''                bootstrap.reservations_released,
            )
''',
    '''                bootstrap.reservations_released,
                bootstrap.reconciliation_findings,
                bootstrap.audit_findings,
            )
''',
)
replace_once(
    RUNTIME,
    '''        async with self._cycle_lock:
            try:
                bootstrap = await asyncio.to_thread(
''',
    '''        async with self._cycle_lock:
            try:
                if (
                    self.health.last_cycle_at_ms is not None
                    and current < self.health.last_cycle_at_ms
                ):
                    raise ValueError("portfolio runtime clock moved backwards")
                bootstrap = await asyncio.to_thread(
''',
)
replace_once(
    RUNTIME,
    '''                self.health.reservations_released += bootstrap.reservations_released
                self.health.last_cycle_at_ms = current
''',
    '''                self.health.reservations_released += bootstrap.reservations_released
                self.health.reconciliation_findings += (
                    bootstrap.reconciliation_findings
                )
                self.health.audit_findings += bootstrap.audit_findings
                self.health.last_cycle_at_ms = current
''',
)

replace_once(
    LIFECYCLE_TEST,
    '''    assert result.position.version == position.version + 1
    assert balances == balances
''',
    '''    assert result.position.version == position.version + 1
''',
)

replace_once(
    ENGINE_TEST,
    '''    assert after_withdrawal.equity == after_withdrawal.day_start_equity
    assert after_withdrawal.equity == after_withdrawal.peak_equity
''',
    '''    assert after_withdrawal.equity == after_withdrawal.day_start_equity
    assert after_withdrawal.equity == after_withdrawal.peak_equity

    final_withdrawal = withdrawal_transaction(
        account_id="paper",
        amount=after_withdrawal.balances.cash,
        event_time=NOW + 3,
        source_id="equity-anchor-full-withdrawal",
        policy=engine.policy,
    )
    portfolio_store.apply_external_transaction(final_withdrawal, engine.policy)
    empty = engine.snapshot(observed_at=NOW + 3)
    assert empty.equity == Decimal("0")
    assert empty.day_start_equity == Decimal("0")
    assert empty.peak_equity == Decimal("0")
''',
)

append_once(
    STORE_TEST,
    "def test_snapshot_time_is_monotonic_at_store_boundary(",
    '''def test_snapshot_time_is_monotonic_at_store_boundary(tmp_path) -> None:
    store, _, initialized = initialize_store(tmp_path)
    first = build_account_snapshot(
        account_id="paper",
        observed_at=NOW + 2,
        balances=initialized.balances,
        positions=(),
        day_start_equity=initialized.balances.settled_assets,
        peak_equity=initialized.balances.settled_assets,
        consecutive_losses=0,
        watermarks=AccountWatermarks(),
    )
    committed = store.commit_snapshot(first)

    regressed = build_account_snapshot(
        account_id="paper",
        observed_at=NOW + 1,
        balances=initialized.balances,
        positions=(),
        day_start_equity=initialized.balances.settled_assets,
        peak_equity=initialized.balances.settled_assets,
        consecutive_losses=0,
        watermarks=AccountWatermarks(),
    )
    with pytest.raises(AccountStoreConflict, match="latest snapshot"):
        store.commit_snapshot(regressed)

    assert store.latest_snapshot("paper") == committed.snapshot


def test_account_instance_time_never_regresses_across_streams(tmp_path) -> None:
    from crypto_quant.portfolio_accounting import MarkAccountingResult
    from crypto_quant.portfolio_facts import make_mark_price_fact

    store, policy, _ = initialize_store(tmp_path)
    fact = make_mark_price_fact(
        account_id="paper",
        source_event_id="flat-mark-ahead-of-cash-flow",
        symbol="ETH/USDT:USDT",
        observed_at=NOW + 2,
        source_sequence=1,
        mark_price=50,
    )
    store.commit_mark(MarkAccountingResult(fact, None), policy)
    deposit = deposit_transaction(
        account_id="paper",
        amount="1",
        event_time=NOW + 1,
        source_id="deposit-after-later-mark",
        policy=policy,
    )
    store.apply_external_transaction(deposit, policy)

    instance = store.load_instance("paper")
    assert instance is not None
    assert instance.updated_at == NOW + 2
''',
)

append_once(
    ENGINE_TEST,
    "def test_snapshot_cannot_precede_account_state_or_prior_snapshot(",
    '''def test_snapshot_cannot_precede_account_state_or_prior_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    engine, _, _, _, portfolio_store, _, _ = system(tmp_path)
    monkeypatch.setattr(
        "crypto_quant.portfolio_engine.time.time",
        lambda: (NOW + 100) / 1000,
    )
    engine.apply_mark(
        source_event_id="flat-account-temporal-mark",
        symbol="ETH/USDT:USDT",
        observed_at=NOW + 2,
        source_sequence=1,
        mark_price=50,
    )

    with pytest.raises(PortfolioHealthError, match="precedes account state"):
        engine.snapshot(observed_at=NOW + 1)

    first = engine.snapshot(observed_at=NOW + 3)
    with pytest.raises(PortfolioHealthError, match="moved backwards"):
        engine.snapshot(observed_at=NOW + 2)

    assert portfolio_store.latest_snapshot("paper") == first
''',
)

append_once(
    RUNTIME_TEST,
    "def test_runtime_rejects_clock_rollback_before_processing(",
    '''def test_runtime_rejects_clock_rollback_before_processing(tmp_path) -> None:
    engine, _, portfolio_store, _ = runtime_system(tmp_path)
    runtime = PortfolioRuntime(
        engine,
        PortfolioRuntimeSettings(snapshot_interval_ms=0),
        clock=lambda: NOW + 3,
    )
    asyncio.run(runtime.run_once())
    journal_count = portfolio_store.metrics("paper")["journal_transactions"]
    cycles = runtime.health.cycles

    with pytest.raises(ValueError, match="clock moved backwards"):
        asyncio.run(runtime.run_once(now_ms=NOW + 2))

    assert runtime.health.cycles == cycles
    assert runtime.health.failures == 1
    assert portfolio_store.metrics("paper")["journal_transactions"] == journal_count
''',
)
