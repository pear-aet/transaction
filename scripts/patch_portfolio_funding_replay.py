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
        f"expected one funding replay source or existing target in {path}, found {count}"
    )


def append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


ACCOUNTING = Path("src/crypto_quant/portfolio_accounting.py")
STORE = Path("src/crypto_quant/portfolio_store.py")
ENGINE = Path("src/crypto_quant/portfolio_engine.py")
RESEARCH_ACCOUNTING = Path("src/crypto_quant/research_accounting.py")
ENGINE_TEST = Path("tests/test_portfolio_engine.py")
ACCOUNTING_TEST = Path("tests/test_portfolio_lifecycle.py")

replace_once(
    ACCOUNTING,
    '''class FundingAccountingResult:
    position: PortfolioPosition
    transaction: JournalTransaction
    signed_funding_pnl: Decimal
''',
    '''class FundingAccountingResult:
    position: PortfolioPosition
    transaction: JournalTransaction | None
    signed_funding_pnl: Decimal
    rate: Decimal
    mark_price: Decimal
    event_time: int
    source_id: str
''',
)
replace_once(
    ACCOUNTING,
    '''    transaction = funding_transaction(
        account_id=account_id,
        position=position,
        rate=selected_rate,
        mark_price=selected_mark,
        event_time=event_time,
        source_id=source_id,
        policy=policy,
    )
    next_position = replace(
        position,
        mark_price=selected_mark,
        funding_pnl=position.funding_pnl + signed_pnl,
        last_funding_time=event_time,
        updated_at=event_time,
        version=position.version + 1,
    )
    return FundingAccountingResult(next_position, transaction, signed_pnl)
''',
    '''    transaction = None
    if payment != ZERO:
        transaction = funding_transaction(
            account_id=account_id,
            position=position,
            rate=selected_rate,
            mark_price=selected_mark,
            event_time=event_time,
            source_id=source_id,
            policy=policy,
        )
    next_position = replace(
        position,
        mark_price=selected_mark,
        funding_pnl=position.funding_pnl + signed_pnl,
        last_funding_time=event_time,
        updated_at=event_time,
        version=position.version + 1,
    )
    return FundingAccountingResult(
        next_position,
        transaction,
        signed_pnl,
        selected_rate,
        selected_mark,
        event_time,
        source_id,
    )
''',
)

replace_once(
    STORE,
    '''from .portfolio_contracts import (
''',
    '''from .portfolio_contracts import (
    ZERO,
    as_decimal,
''',
)
replace_once(
    STORE,
    '''class FundingCommit:
    position: PortfolioPosition
    balances: AccountBalances
    transaction: JournalTransaction
    duplicate: bool
''',
    '''class FundingCommit:
    position: PortfolioPosition
    balances: AccountBalances
    transaction: JournalTransaction | None
    signed_funding_pnl: Decimal
    rate: Decimal
    mark_price: Decimal
    event_time: int
    source_id: str
    duplicate: bool
''',
)
replace_once(
    STORE,
    '''                    position_version INTEGER NOT NULL CHECK(position_version > 0),
                    event_time INTEGER NOT NULL CHECK(event_time >= 0),
                    transaction_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
''',
    '''                    position_version INTEGER NOT NULL CHECK(position_version > 0),
                    event_time INTEGER NOT NULL CHECK(event_time >= 0),
                    transaction_id TEXT UNIQUE,
                    payload TEXT NOT NULL,
''',
)
replace_once(
    STORE,
    '''    def commit_funding(
        self,
        *,
        account_id: str,
        symbol: str,
        source_id: str,
        result: FundingAccountingResult,
        policy: AccountPolicy,
    ) -> FundingCommit:
        transaction = result.transaction
        if transaction.source_kind is not JournalSourceKind.FUNDING:
            raise ValueError("funding transaction has the wrong source kind")
        if transaction.account_id != account_id or transaction.source_id != source_id:
            raise ValueError("funding transaction identity mismatch")
        if result.position.account_id != account_id or result.position.symbol != symbol:
            raise ValueError("funding result belongs to another position")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if instance_row is None:
                raise KeyError(f"account not found: {account_id}")
            instance = self._instance_from_row(instance_row)
            self._assert_instance(instance, policy)
            existing = connection.execute(
                "SELECT * FROM account_funding_applications WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if existing is not None:
                stored = self._load_journal_by_id(
                    connection,
                    str(existing["transaction_id"]),
                )
                current = self._stored_position(connection, account_id, symbol)
                if (
                    stored is None
                    or not self._same_transaction(stored, transaction)
                    or current is None
                    or current.version < int(existing["position_version"])
                ):
                    raise AccountStoreConflict("funding source changed its durable facts")
                balances, _, _ = self._balances(connection, account_id)
                return FundingCommit(current, balances, stored, True)
            current = self._stored_position(connection, account_id, symbol)
            if current is None:
                raise AccountStoreConflict("funding attempted to update a missing position")
            if current.position_id != result.position.position_id:
                raise AccountStoreConflict("funding position identity changed")
            if current.version + 1 != result.position.version:
                raise AccountStoreConflict("stale or skipped funding position version")
            stored, balances, duplicate = self._commit_transaction(connection, transaction)
            if duplicate:
                raise AccountStoreConflict("funding journal exists without application row")
            connection.execute(
                """UPDATE account_positions SET
                    version=?,updated_at=?,payload=?
                    WHERE account_id=? AND symbol=?""",
                (
                    result.position.version,
                    result.position.updated_at,
                    self._encode_position(result.position),
                    account_id,
                    symbol,
                ),
            )
            connection.execute(
                """INSERT INTO account_funding_applications(
                    source_id,account_id,symbol,position_id,position_version,
                    event_time,transaction_id,payload
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    source_id,
                    account_id,
                    symbol,
                    result.position.position_id,
                    result.position.version,
                    transaction.event_time,
                    transaction.transaction_id,
                    self._encode(
                        {
                            "signed_funding_pnl": str(result.signed_funding_pnl),
                            "position": result.position.to_dict(),
                        }
                    ),
                ),
            )
            connection.execute(
                "UPDATE account_instances SET updated_at=? WHERE account_id=?",
                (transaction.event_time, account_id),
            )
        return FundingCommit(result.position, balances, stored, False)
''',
    '''    def commit_funding(
        self,
        *,
        account_id: str,
        symbol: str,
        source_id: str,
        result: FundingAccountingResult,
        policy: AccountPolicy,
    ) -> FundingCommit:
        transaction = result.transaction
        if result.source_id != source_id or result.event_time < 0:
            raise ValueError("funding result source identity mismatch")
        if result.position.account_id != account_id or result.position.symbol != symbol:
            raise ValueError("funding result belongs to another position")
        if result.position.last_funding_time != result.event_time:
            raise ValueError("funding result time differs from its position")
        if transaction is None:
            if result.signed_funding_pnl != ZERO:
                raise ValueError("non-zero funding result requires a journal transaction")
        else:
            if transaction.source_kind is not JournalSourceKind.FUNDING:
                raise ValueError("funding transaction has the wrong source kind")
            if (
                transaction.account_id != account_id
                or transaction.source_id != source_id
                or transaction.event_time != result.event_time
            ):
                raise ValueError("funding transaction identity mismatch")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if instance_row is None:
                raise KeyError(f"account not found: {account_id}")
            instance = self._instance_from_row(instance_row)
            self._assert_instance(instance, policy)
            existing = connection.execute(
                "SELECT * FROM account_funding_applications WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if existing is not None:
                payload = json.loads(str(existing["payload"]))
                position_payload = payload.get("position")
                if not isinstance(position_payload, dict):
                    raise AccountStoreConflict("funding application lost its position payload")
                historical_position = PortfolioPosition.from_dict(position_payload)
                stored_signed = as_decimal(
                    payload.get("signed_funding_pnl"),
                    "stored signed_funding_pnl",
                )
                stored_rate = as_decimal(payload.get("rate"), "stored funding rate")
                stored_mark = as_decimal(
                    payload.get("mark_price"),
                    "stored funding mark price",
                )
                transaction_id = existing["transaction_id"]
                stored_transaction = (
                    None
                    if transaction_id is None
                    else self._load_journal_by_id(connection, str(transaction_id))
                )
                durable_identity = (
                    historical_position.account_id,
                    historical_position.symbol,
                    historical_position.position_id,
                    historical_position.version,
                    int(existing["event_time"]),
                    stored_signed,
                    stored_rate,
                    stored_mark,
                )
                requested_identity = (
                    account_id,
                    symbol,
                    result.position.position_id,
                    result.position.version,
                    result.event_time,
                    result.signed_funding_pnl,
                    result.rate,
                    result.mark_price,
                )
                if durable_identity != requested_identity:
                    raise AccountStoreConflict("funding source changed its durable facts")
                if transaction is None:
                    if transaction_id is not None or stored_transaction is not None:
                        raise AccountStoreConflict(
                            "zero funding application unexpectedly owns a journal"
                        )
                elif (
                    transaction_id is None
                    or stored_transaction is None
                    or not self._same_transaction(stored_transaction, transaction)
                ):
                    raise AccountStoreConflict("funding source changed its transaction")
                balances, _, _ = self._balances(connection, account_id)
                return FundingCommit(
                    historical_position,
                    balances,
                    stored_transaction,
                    stored_signed,
                    stored_rate,
                    stored_mark,
                    result.event_time,
                    source_id,
                    True,
                )
            current = self._stored_position(connection, account_id, symbol)
            if current is None:
                raise AccountStoreConflict("funding attempted to update a missing position")
            if current.position_id != result.position.position_id:
                raise AccountStoreConflict("funding position identity changed")
            if current.version + 1 != result.position.version:
                raise AccountStoreConflict("stale or skipped funding position version")
            if transaction is None:
                balances, _, _ = self._balances(connection, account_id)
                stored_transaction = None
            else:
                stored_transaction, balances, duplicate = self._commit_transaction(
                    connection,
                    transaction,
                )
                if duplicate:
                    raise AccountStoreConflict(
                        "funding journal exists without application row"
                    )
            connection.execute(
                """UPDATE account_positions SET
                    version=?,updated_at=?,payload=?
                    WHERE account_id=? AND symbol=?""",
                (
                    result.position.version,
                    result.position.updated_at,
                    self._encode_position(result.position),
                    account_id,
                    symbol,
                ),
            )
            connection.execute(
                """INSERT INTO account_funding_applications(
                    source_id,account_id,symbol,position_id,position_version,
                    event_time,transaction_id,payload
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    source_id,
                    account_id,
                    symbol,
                    result.position.position_id,
                    result.position.version,
                    result.event_time,
                    None if stored_transaction is None else stored_transaction.transaction_id,
                    self._encode(
                        {
                            "signed_funding_pnl": str(result.signed_funding_pnl),
                            "rate": str(result.rate),
                            "mark_price": str(result.mark_price),
                            "position": result.position.to_dict(),
                        }
                    ),
                ),
            )
            connection.execute(
                "UPDATE account_instances SET updated_at=? WHERE account_id=?",
                (result.event_time, account_id),
            )
        return FundingCommit(
            result.position,
            balances,
            stored_transaction,
            result.signed_funding_pnl,
            result.rate,
            result.mark_price,
            result.event_time,
            source_id,
            False,
        )
''',
)

append_once(
    STORE,
    "    def load_funding_commit(",
    '''    def load_funding_commit(
        self,
        *,
        account_id: str,
        symbol: str,
        source_id: str,
    ) -> FundingCommit | None:
        if not account_id or not symbol or not source_id:
            raise ValueError("funding lookup identifiers must not be empty")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM account_funding_applications WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                return None
            if str(row["account_id"]) != account_id or str(row["symbol"]) != symbol:
                raise AccountStoreConflict("funding source belongs to another account or symbol")
            payload = json.loads(str(row["payload"]))
            position_payload = payload.get("position")
            if not isinstance(position_payload, dict):
                raise AccountStoreConflict("funding application lost its position payload")
            position = PortfolioPosition.from_dict(position_payload)
            signed = as_decimal(
                payload.get("signed_funding_pnl"),
                "stored signed_funding_pnl",
            )
            rate = as_decimal(payload.get("rate"), "stored funding rate")
            mark = as_decimal(payload.get("mark_price"), "stored funding mark price")
            event_time = int(row["event_time"])
            transaction_id = row["transaction_id"]
            transaction = (
                None
                if transaction_id is None
                else self._load_journal_by_id(connection, str(transaction_id))
            )
            if (
                position.account_id != account_id
                or position.symbol != symbol
                or position.position_id != str(row["position_id"])
                or position.version != int(row["position_version"])
                or position.last_funding_time != event_time
            ):
                raise AccountStoreConflict("funding application durable facts disagree")
            if transaction is None:
                if transaction_id is not None or signed != ZERO:
                    raise AccountStoreConflict("funding application lost its journal")
            elif (
                transaction.account_id != account_id
                or transaction.source_id != source_id
                or transaction.event_time != event_time
                or transaction.source_kind is not JournalSourceKind.FUNDING
            ):
                raise AccountStoreConflict("funding journal differs from its application")
            balances, _, _ = self._balances(connection, account_id)
        return FundingCommit(
            position,
            balances,
            transaction,
            signed,
            rate,
            mark,
            event_time,
            source_id,
            True,
        )
''',
)

replace_once(
    ENGINE,
    '''    FillCommit,
    MarkCommit,
''',
    '''    FillCommit,
    FundingCommit,
    MarkCommit,
''',
)
replace_once(
    ENGINE,
    '''    ) -> Any:
        self._assert_initialized(require_active=False)
        position = self.store.load_position(self.settings.account_id, symbol)
        if position is None:
            return None
        result = apply_funding_to_position(
''',
    '''    ) -> FundingCommit | None:
        self._assert_initialized(require_active=False)
        existing = self.store.load_funding_commit(
            account_id=self.settings.account_id,
            symbol=symbol,
            source_id=source_id,
        )
        if existing is not None:
            if (
                existing.event_time != event_time
                or existing.source_id != source_id
                or existing.rate != as_decimal(rate, "funding rate")
                or existing.mark_price
                != as_decimal(mark_price, "funding mark price")
            ):
                raise PortfolioSourceError(
                    "funding source changed immutable semantics"
                )
            self.health.last_error = None
            return existing
        position = self.store.load_position(self.settings.account_id, symbol)
        if position is None:
            return None
        result = apply_funding_to_position(
''',
)

replace_once(
    RESEARCH_ACCOUNTING,
    '''        self._commit_transaction(result.transaction)
        self.positions[symbol] = result.position
''',
    '''        if result.transaction is not None:
            self._commit_transaction(result.transaction)
        self.positions[symbol] = result.position
''',
)

append_once(
    ACCOUNTING_TEST,
    "def test_zero_funding_advances_position_without_fake_journal(",
    '''def test_zero_funding_advances_position_without_fake_journal() -> None:
    policy, balances, _, _, opened = opened_position()
    position = opened.position
    assert position is not None

    result = apply_funding_to_position(
        account_id="paper",
        position=position,
        rate=Decimal("0"),
        mark_price=Decimal("101"),
        event_time=NOW + 3,
        source_id="zero-funding-accounting",
        policy=policy,
    )

    assert result.transaction is None
    assert result.signed_funding_pnl == Decimal("0")
    assert result.position.funding_pnl == position.funding_pnl
    assert result.position.last_funding_time == NOW + 3
    assert result.position.updated_at == NOW + 3
    assert result.position.version == position.version + 1
    assert balances == balances
''',
)

append_once(
    ENGINE_TEST,
    "def test_funding_source_replay_is_idempotent_and_immutable(",
    '''def test_funding_source_replay_is_idempotent_and_immutable(tmp_path) -> None:
    (
        engine,
        _,
        _,
        execution_store,
        portfolio_store,
        _,
        stored_command,
    ) = system(tmp_path)
    dispatch_and_fill_primary(execution_store, stored_command)
    engine.bootstrap(now_ms=NOW + 3)

    first = engine.apply_funding(
        source_id="portfolio-funding-replay-1",
        symbol=stored_command.command.symbol,
        event_time=NOW + 4,
        rate=Decimal("0.0001"),
        mark_price=Decimal("101"),
    )
    assert first is not None
    assert first.duplicate is False
    assert first.transaction is not None
    balances_after_first = portfolio_store.balances("paper")
    position_after_first = portfolio_store.load_position(
        "paper",
        stored_command.command.symbol,
    )

    replay = engine.apply_funding(
        source_id="portfolio-funding-replay-1",
        symbol=stored_command.command.symbol,
        event_time=NOW + 4,
        rate=Decimal("0.0001"),
        mark_price=Decimal("101"),
    )
    assert replay is not None
    assert replay.duplicate is True
    assert replay.position == first.position
    assert portfolio_store.balances("paper") == balances_after_first
    assert (
        portfolio_store.load_position("paper", stored_command.command.symbol)
        == position_after_first
    )
    assert engine.health.funding_applied == 1

    with pytest.raises(PortfolioSourceError, match="funding source changed"):
        engine.apply_funding(
            source_id="portfolio-funding-replay-1",
            symbol=stored_command.command.symbol,
            event_time=NOW + 4,
            rate=Decimal("0.0002"),
            mark_price=Decimal("101"),
        )


def test_zero_funding_is_persisted_and_replayed_without_journal(tmp_path) -> None:
    (
        engine,
        _,
        _,
        execution_store,
        portfolio_store,
        _,
        stored_command,
    ) = system(tmp_path)
    dispatch_and_fill_primary(execution_store, stored_command)
    engine.bootstrap(now_ms=NOW + 3)
    journal_before = portfolio_store.metrics("paper")["journal_transactions"]
    balances_before = portfolio_store.balances("paper")

    first = engine.apply_funding(
        source_id="portfolio-zero-funding-1",
        symbol=stored_command.command.symbol,
        event_time=NOW + 4,
        rate=Decimal("0"),
        mark_price=Decimal("101"),
    )
    assert first is not None
    assert first.duplicate is False
    assert first.transaction is None
    assert first.signed_funding_pnl == Decimal("0")
    assert first.position.last_funding_time == NOW + 4
    assert portfolio_store.balances("paper") == balances_before
    assert portfolio_store.metrics("paper")["journal_transactions"] == journal_before

    replay = engine.apply_funding(
        source_id="portfolio-zero-funding-1",
        symbol=stored_command.command.symbol,
        event_time=NOW + 4,
        rate=Decimal("0"),
        mark_price=Decimal("101"),
    )
    assert replay is not None
    assert replay.duplicate is True
    assert replay.transaction is None
    assert replay.position == first.position
    assert portfolio_store.metrics("paper")["journal_transactions"] == journal_before

    with pytest.raises(PortfolioSourceError, match="funding source changed"):
        engine.apply_funding(
            source_id="portfolio-zero-funding-1",
            symbol=stored_command.command.symbol,
            event_time=NOW + 4,
            rate=Decimal("0.0001"),
            mark_price=Decimal("101"),
        )
''',
)
