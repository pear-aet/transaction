from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 1:
        path.write_text(text.replace(old, new), encoding="utf-8")
        return
    if count == 0 and new in text:
        return
    raise SystemExit(
        f"expected one Portfolio boundary source or existing target in {path}, found {count}"
    )


def append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


STORE = Path("src/crypto_quant/portfolio_store.py")
ENGINE = Path("src/crypto_quant/portfolio_engine.py")
FIXTURES = Path("tests/portfolio_fixtures.py")
ENGINE_TEST = Path("tests/test_portfolio_engine.py")
STORE_TEST = Path("tests/test_portfolio_lifecycle_store.py")

append_once(
    STORE,
    "    def _assert_entry_reservation_available(",
    '''    def _assert_entry_reservation_available(
        self,
        connection,
        reservation: CommandReservation,
    ) -> None:
        if not reservation.entry or reservation.state is not ReservationState.ACTIVE:
            return
        rows = connection.execute(
            """SELECT * FROM account_reservations
            WHERE account_id=? AND symbol=? AND state='active'
            ORDER BY storage_sequence""",
            (reservation.account_id, reservation.symbol),
        ).fetchall()
        for row in rows:
            existing = self._decode_reservation(
                str(row["payload"]),
                int(row["storage_sequence"]),
            )
            if existing.entry and existing.command_id != reservation.command_id:
                raise AccountStoreConflict(
                    "active entry reservation already exists for account and symbol"
                )
''',
)
replace_once(
    STORE,
    '''                return ReservationCommit(existing, balances, stored_transaction, True)

            transaction = None
''',
    '''                return ReservationCommit(existing, balances, stored_transaction, True)

            self._assert_entry_reservation_available(connection, reservation)
            transaction = None
''',
)
replace_once(
    STORE,
    '''            if existing_row is not None:
                raise AccountStoreConflict(
                    "reservation exists without its atomic execution command event"
                )

            transaction = None
''',
    '''            if existing_row is not None:
                raise AccountStoreConflict(
                    "reservation exists without its atomic execution command event"
                )

            self._assert_entry_reservation_available(connection, reservation)
            transaction = None
''',
)

replace_once(
    ENGINE,
    '''    def snapshot(self, *, observed_at: int | None = None) -> AccountSnapshot:
        current = int(time.time() * 1000) if observed_at is None else observed_at
        if current < 0:
            raise ValueError("observed_at must be non-negative")
        instance = self.store.load_instance(self.settings.account_id)
''',
    '''    def snapshot(self, *, observed_at: int | None = None) -> AccountSnapshot:
        wall_now = int(time.time() * 1000)
        current = wall_now if observed_at is None else observed_at
        if current < 0:
            raise ValueError("observed_at must be non-negative")
        if current > wall_now:
            raise PortfolioHealthError("portfolio snapshot time cannot be in the future")
        instance = self.store.load_instance(self.settings.account_id)
''',
)

replace_once(
    FIXTURES,
    '''def strategy_intent(action: StrategyAction = StrategyAction.ENTER_LONG) -> StrategyIntent:
''',
    '''def strategy_intent(
    action: StrategyAction = StrategyAction.ENTER_LONG,
    *,
    identity_suffix: str = "",
) -> StrategyIntent:
''',
)
replace_once(
    FIXTURES,
    '''        intent_id=f"portfolio-intent-{action.value}",
''',
    '''        intent_id=f"portfolio-intent-{action.value}{identity_suffix}",
''',
)
replace_once(
    FIXTURES,
    '''        source_event_id=f"portfolio-market-{action.value}",
''',
    '''        source_event_id=f"portfolio-market-{action.value}{identity_suffix}",
''',
)
replace_once(
    FIXTURES,
    '''    *,
    storage_sequence: int = 1,
    position_side: str = "long",
''',
    '''    *,
    storage_sequence: int = 1,
    identity_suffix: str = "",
    position_side: str = "long",
''',
)
replace_once(
    FIXTURES,
    '''        f"portfolio-account-{action.value}",
''',
    '''        f"portfolio-account-{action.value}{identity_suffix}",
''',
)
replace_once(
    FIXTURES,
    '''        f"portfolio-risk-market-{action.value}",
''',
    '''        f"portfolio-risk-market-{action.value}{identity_suffix}",
''',
)
replace_once(
    FIXTURES,
    '''            strategy_intent(action),
''',
    '''            strategy_intent(action, identity_suffix=identity_suffix),
''',
)

replace_once(
    ENGINE_TEST,
    '''def persisted_decision(risk_store: RiskStore, *, sequence_hint: int = 1):
    fixture = risk_decision(storage_sequence=sequence_hint)
''',
    '''def persisted_decision(
    risk_store: RiskStore,
    *,
    sequence_hint: int = 1,
    identity_suffix: str = "",
):
    fixture = risk_decision(
        storage_sequence=sequence_hint,
        identity_suffix=identity_suffix,
    )
''',
)
replace_once(
    ENGINE_TEST,
    '''    PortfolioEngine,
    PortfolioEngineSettings,
    PortfolioSourceError,
''',
    '''    PortfolioEngine,
    PortfolioEngineSettings,
    PortfolioHealthError,
    PortfolioSourceError,
''',
)
replace_once(
    ENGINE_TEST,
    '''from crypto_quant.portfolio_store import PortfolioStore
''',
    '''from crypto_quant.portfolio_store import AccountStoreConflict, PortfolioStore
''',
)
append_once(
    ENGINE_TEST,
    "def test_conflicting_entry_reservation_is_rejected_atomically(",
    '''def test_conflicting_entry_reservation_is_rejected_atomically(tmp_path) -> None:
    (
        engine,
        _,
        risk_store,
        execution_store,
        portfolio_store,
        _,
        stored_command,
    ) = system(tmp_path)
    first = engine.process_command(stored_command, processed_at=NOW + 1)
    assert first.reservation is not None
    balances_after_first = portfolio_store.balances("paper")

    second_decision = persisted_decision(
        risk_store,
        sequence_hint=2,
        identity_suffix="-second-entry",
    )
    second_command = execution_command(second_decision)
    second_stored = execution_store.commit_command(
        second_command,
        updated_at=NOW + 1,
    ).stored_command

    with pytest.raises(AccountStoreConflict, match="active entry reservation"):
        engine.process_command(second_stored, processed_at=NOW + 2)

    assert portfolio_store.balances("paper") == balances_after_first
    assert portfolio_store.load_reservation(second_command.command_id) is None
    assert portfolio_store.load_execution_command_event(
        "paper",
        second_command.command_id,
    ) is None
    assert portfolio_store.latest_execution_command_sequence("paper") == 1


def test_future_snapshot_fails_closed_without_positions(tmp_path, monkeypatch) -> None:
    engine, *_ = system(tmp_path)
    monkeypatch.setattr(
        "crypto_quant.portfolio_engine.time.time",
        lambda: NOW / 1000,
    )
    with pytest.raises(PortfolioHealthError, match="future"):
        engine.snapshot(observed_at=NOW + 1)
''',
)

append_once(
    STORE_TEST,
    "def test_store_rejects_second_active_entry_reservation(",
    '''def test_store_rejects_second_active_entry_reservation(tmp_path) -> None:
    store = PortfolioStore(tmp_path / "reservation-conflict.db")
    policy = AccountPolicy()
    initialized = store.initialize_account(
        account_id="paper",
        policy=policy,
        transaction=initialize_account_transaction(
            account_id="paper",
            initial_capital="10000",
            event_time=NOW - 1,
            source_id="reservation-conflict-capital",
            policy=policy,
        ),
    )
    first_decision = risk_decision(identity_suffix="-first-entry")
    first_command = execution_command(first_decision)
    first = store.commit_reservation(
        plan_command_reservation(
            account_id="paper",
            command=first_command,
            decision=first_decision,
            balances=initialized.balances,
            policy=policy,
            now_ms=NOW,
        ),
        policy,
    )

    second_decision = risk_decision(
        storage_sequence=2,
        identity_suffix="-second-entry",
    )
    second_command = execution_command(second_decision)
    second_plan = plan_command_reservation(
        account_id="paper",
        command=second_command,
        decision=second_decision,
        balances=first.balances,
        policy=policy,
        now_ms=NOW + 1,
    )
    with pytest.raises(AccountStoreConflict, match="active entry reservation"):
        store.commit_reservation(second_plan, policy)

    assert store.load_reservation(second_command.command_id) is None
    assert len(store.active_reservations("paper", symbol=second_command.symbol)) == 1
    assert len(store.journal_transactions("paper")) == 2
''',
)
