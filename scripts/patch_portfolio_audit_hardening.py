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
        f"expected one Portfolio audit source or existing target, found {count}"
    )


def append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


store = Path("src/crypto_quant/portfolio_store.py")
replace_once(
    store,
    '''        if transaction.event_time < updated_at:
            raise AccountStoreConflict("journal event time moved behind the account checkpoint")
        next_balances = balances.apply(transaction)
''',
    '''        # The durable journal sequence is the cross-stream ordering key.
        # Exchange event times are monotonic only inside their own source stream.
        next_balances = balances.apply(transaction)
''',
)
replace_once(
    store,
    '''                self._encode(next_balances.to_dict()),
                sequence,
                transaction.event_time,
                transaction.account_id,
''',
    '''                self._encode(next_balances.to_dict()),
                sequence,
                max(updated_at, transaction.event_time),
                transaction.account_id,
''',
)

path = Path("src/crypto_quant/portfolio_audit.py")
replace_once(
    path,
    '''        prior_sequence = 0
        prior_event_time = -1
        latest_sequence = 0
''',
    '''        prior_sequence = 0
        latest_sequence = 0
''',
)
replace_once(
    path,
    '''                if sequence <= prior_sequence:
                    raise PortfolioAuditError("journal storage sequence did not advance")
                if transaction.event_time < prior_event_time:
                    raise PortfolioAuditError("journal event time moved backwards")
                if rebuilt is not None:
''',
    '''                if sequence <= prior_sequence:
                    raise PortfolioAuditError("journal storage sequence did not advance")
                if rebuilt is not None:
''',
)
replace_once(
    path,
    '''                prior_sequence = sequence
                prior_event_time = transaction.event_time
                latest_sequence = sequence
                latest_event_time = transaction.event_time
''',
    '''                prior_sequence = sequence
                latest_sequence = sequence
                latest_event_time = max(latest_event_time, transaction.event_time)
''',
)
replace_once(
    path,
    '''        with self._connect() as connection:
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if instance_row is None:
                raise PortfolioAuditError(f"account not found: {account_id}")
            state_time = int(instance_row["updated_at"])
''',
    '''        with self._connect() as connection:
            connection.execute("BEGIN")
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if instance_row is None:
                raise PortfolioAuditError(f"account not found: {account_id}")
            state_time = int(instance_row["updated_at"])
            if observed_at < state_time:
                raise ValueError("Portfolio audit cannot precede account state")
''',
)

append_once(
    Path("tests/test_portfolio_store.py"),
    "def test_cross_stream_journal_uses_storage_sequence_not_global_event_time(",
    '''def test_cross_stream_journal_uses_storage_sequence_not_global_event_time(
    tmp_path,
) -> None:
    store, policy, _ = initialize_store(tmp_path)
    later = deposit_transaction(
        account_id="paper",
        amount="10",
        event_time=NOW + 2,
        source_id="later-stream-event",
        policy=policy,
    )
    earlier = deposit_transaction(
        account_id="paper",
        amount="5",
        event_time=NOW + 1,
        source_id="earlier-stream-event-arrived-late",
        policy=policy,
    )

    first = store.apply_external_transaction(later, policy)
    second = store.apply_external_transaction(earlier, policy)

    assert first.duplicate is False
    assert second.duplicate is False
    journal = store.journal_transactions("paper")
    assert [item.event_time for item in journal[-2:]] == [NOW + 2, NOW + 1]
    assert journal[-2].storage_sequence is not None
    assert journal[-1].storage_sequence is not None
    assert journal[-2].storage_sequence < journal[-1].storage_sequence
    assert store.balances("paper").cash == Decimal("10015.00000000")
    instance = store.load_instance("paper")
    assert instance is not None
    assert instance.updated_at == NOW + 2
''',
)

append_once(
    Path("tests/test_portfolio_audit.py"),
    "def test_auditor_accepts_cross_stream_event_time_interleaving(",
    '''def test_auditor_accepts_cross_stream_event_time_interleaving(tmp_path) -> None:
    from crypto_quant.portfolio_accounting import deposit_transaction

    store = PortfolioStore(tmp_path / "portfolio-audit-interleaving.db")
    policy = AccountPolicy()
    store.initialize_account(
        account_id="paper",
        policy=policy,
        transaction=initialize_account_transaction(
            account_id="paper",
            initial_capital="10000",
            event_time=NOW - 1,
            source_id="audit-interleaving-capital",
            policy=policy,
        ),
    )
    store.apply_external_transaction(
        deposit_transaction(
            account_id="paper",
            amount="10",
            event_time=NOW + 2,
            source_id="audit-later-stream-event",
            policy=policy,
        ),
        policy,
    )
    store.apply_external_transaction(
        deposit_transaction(
            account_id="paper",
            amount="5",
            event_time=NOW + 1,
            source_id="audit-earlier-stream-event-arrived-late",
            policy=policy,
        ),
        policy,
    )

    report = PortfolioAuditor(store, policy).audit(
        "paper",
        observed_at=NOW + 2,
        persist_findings=False,
    )
    assert report.healthy is True
    assert report.latest_journal_sequence == 3
''',
)
