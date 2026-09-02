from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new:
        target_count = text.count(new)
        if target_count == 1:
            return
        if target_count > 1:
            raise SystemExit(
                f"Portfolio snapshot patch {label} found multiple existing targets: "
                f"{target_count}"
            )
    source_count = text.count(old)
    if source_count == 1:
        path.write_text(text.replace(old, new), encoding="utf-8")
        return
    if not new and source_count == 0:
        return
    raise SystemExit(
        f"Portfolio snapshot patch {label} expected one source or existing target, "
        f"found source={source_count} target={0 if not new else text.count(new)}"
    )


def append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


# Available-balance collateral semantics intentionally live in
# patch_portfolio_available_balance.py. This patch owns only halt reasons,
# stale-mark certification and external-capital equity-anchor rebasing.
path = Path("src/crypto_quant/portfolio_engine.py")
replace_once(
    path,
    '''        positions = tuple(self.store.list_positions(self.settings.account_id))
        halt_reasons = []
        if instance.status is AccountStatus.HALTED:
            halt_reasons.append(AccountHaltReason.RECONCILIATION_FAILURE)
''',
    '''        positions = tuple(self.store.list_positions(self.settings.account_id))
        halt_reasons = []
        if instance.status is AccountStatus.HALTED:
            reason = instance.halt_reason or ""
            if reason.startswith("reconciliation:"):
                halt_reasons.append(AccountHaltReason.RECONCILIATION_FAILURE)
            elif "accounting" in reason:
                halt_reasons.append(AccountHaltReason.ACCOUNTING_INVARIANT)
            elif "negative" in reason:
                halt_reasons.append(AccountHaltReason.NEGATIVE_ASSET)
            elif "execution" in reason:
                halt_reasons.append(AccountHaltReason.EXECUTION_CONFLICT)
            else:
                halt_reasons.append(AccountHaltReason.RECONCILIATION_FAILURE)
''',
    "halt_reason_mapping",
)
replace_once(
    path,
    '''            if mark.observed_at > current:
                halt_reasons.append(AccountHaltReason.STALE_MARK)
            elif current - mark.observed_at > self.policy.max_mark_age_ms:
                halt_reasons.append(AccountHaltReason.STALE_MARK)
''',
    '''            if mark.observed_at > current:
                halt_reasons.append(AccountHaltReason.STALE_MARK)
            elif current - mark.observed_at > self.policy.max_mark_age_ms:
                halt_reasons.append(AccountHaltReason.STALE_MARK)
            elif (
                mark.observed_at != position.updated_at
                or mark.mark_price != position.mark_price
            ):
                # A pre-fill or pre-funding mark must not certify a position
                # whose economic state changed afterwards.
                halt_reasons.append(AccountHaltReason.STALE_MARK)
''',
    "stale_mark_certification",
)
replace_once(
    path,
    '''        balances = self.store.balances(self.settings.account_id)
        provisional = build_account_snapshot(
''',
    '''        balances = self.store.balances(self.settings.account_id)
        if prior is None:
            adjusted_day_start = balances.settled_assets
            adjusted_peak = balances.settled_assets
        else:
            external_capital_delta = (
                balances.external_capital - prior.balances.external_capital
            )
            adjusted_day_start = (
                prior.day_start_equity + external_capital_delta
                if self._utc_day(prior.observed_at) == self._utc_day(current)
                else prior.equity + external_capital_delta
            )
            adjusted_peak = prior.peak_equity + external_capital_delta
            if adjusted_day_start < ZERO or adjusted_peak < ZERO:
                raise PortfolioHealthError(
                    "external cash flow made Portfolio equity anchors negative"
                )
        provisional = build_account_snapshot(
''',
    "equity_anchor_rebase",
)
replace_once(
    path,
    '''            day_start_equity=(
                balances.settled_assets
                if prior is None
                else (
                    prior.day_start_equity
                    if self._utc_day(prior.observed_at) == self._utc_day(current)
                    else prior.equity
                )
            ),
            peak_equity=(balances.settled_assets if prior is None else prior.peak_equity),
''',
    '''            day_start_equity=adjusted_day_start,
            peak_equity=adjusted_peak,
''',
    "equity_anchor_fields",
)
replace_once(
    path,
    '''        self.health.last_snapshot_id = commit.snapshot.snapshot_id
        self.health.last_error = None
        return commit.snapshot
''',
    '''        self.health.last_snapshot_id = commit.snapshot.snapshot_id
        if AccountHaltReason.RECONCILIATION_FAILURE in commit.snapshot.halt_reasons:
            if self.health.last_error is None:
                self.health.last_error = (
                    "PortfolioHealthError: account halted by reconciliation failure"
                )
        else:
            self.health.last_error = None
        return commit.snapshot
''',
    "health_state",
)

append_once(
    Path("tests/test_portfolio_engine.py"),
    "def test_external_cash_flows_rebase_equity_anchors(",
    '''def test_external_cash_flows_rebase_equity_anchors(
    tmp_path,
    monkeypatch,
) -> None:
    from crypto_quant.portfolio_accounting import (
        deposit_transaction,
        withdrawal_transaction,
    )

    engine, _, _, _, portfolio_store, _, _ = system(tmp_path)
    monkeypatch.setattr(
        "crypto_quant.portfolio_engine.time.time",
        lambda: (NOW + 100) / 1000,
    )

    initial = engine.snapshot(observed_at=NOW)
    deposit = deposit_transaction(
        account_id="paper",
        amount="500",
        event_time=NOW + 1,
        source_id="equity-anchor-deposit",
        policy=engine.policy,
    )
    portfolio_store.apply_external_transaction(deposit, engine.policy)
    after_deposit = engine.snapshot(observed_at=NOW + 1)

    assert after_deposit.equity == initial.equity + Decimal("500")
    assert after_deposit.day_start_equity == initial.day_start_equity + Decimal("500")
    assert after_deposit.peak_equity == initial.peak_equity + Decimal("500")

    withdrawal = withdrawal_transaction(
        account_id="paper",
        amount="250",
        event_time=NOW + 2,
        source_id="equity-anchor-withdrawal",
        policy=engine.policy,
    )
    portfolio_store.apply_external_transaction(withdrawal, engine.policy)
    after_withdrawal = engine.snapshot(observed_at=NOW + 2)

    assert after_withdrawal.equity == after_deposit.equity - Decimal("250")
    assert (
        after_withdrawal.day_start_equity
        == after_deposit.day_start_equity - Decimal("250")
    )
    assert after_withdrawal.peak_equity == after_deposit.peak_equity - Decimal("250")
    assert after_withdrawal.equity == after_withdrawal.day_start_equity
    assert after_withdrawal.equity == after_withdrawal.peak_equity


def test_pre_position_mark_cannot_certify_new_exposure(
    tmp_path,
    monkeypatch,
) -> None:
    (
        engine,
        _,
        _,
        execution_store,
        _,
        _,
        stored_command,
    ) = system(tmp_path)
    monkeypatch.setattr(
        "crypto_quant.portfolio_engine.time.time",
        lambda: (NOW + 100) / 1000,
    )
    engine.apply_mark(
        source_event_id="mark-before-position",
        symbol=stored_command.command.symbol,
        observed_at=NOW,
        source_sequence=1,
        mark_price=100,
    )
    dispatch_and_fill_primary(execution_store, stored_command)
    engine.bootstrap(now_ms=NOW + 3)

    stale = engine.snapshot(observed_at=NOW + 3)
    assert stale.global_halt is True
    assert AccountHaltReason.STALE_MARK in stale.halt_reasons

    engine.apply_mark(
        source_event_id="mark-after-position",
        symbol=stored_command.command.symbol,
        observed_at=NOW + 4,
        source_sequence=2,
        mark_price=101,
    )
    healthy = engine.snapshot(observed_at=NOW + 4)
    assert healthy.global_halt is False
    assert AccountHaltReason.STALE_MARK not in healthy.halt_reasons
''',
)
