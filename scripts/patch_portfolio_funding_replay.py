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


STORE = Path("src/crypto_quant/portfolio_store.py")
ENGINE = Path("src/crypto_quant/portfolio_engine.py")
TEST = Path("tests/test_portfolio_engine.py")

replace_once(
    STORE,
    '''            if existing is not None:
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
''',
    '''            if existing is not None:
                stored = self._load_journal_by_id(
                    connection,
                    str(existing["transaction_id"]),
                )
                payload = json.loads(str(existing["payload"]))
                position_payload = payload.get("position")
                if not isinstance(position_payload, dict):
                    raise AccountStoreConflict("funding application lost its position payload")
                historical_position = PortfolioPosition.from_dict(position_payload)
                if (
                    stored is None
                    or not self._same_transaction(stored, transaction)
                    or historical_position.account_id != account_id
                    or historical_position.symbol != symbol
                    or historical_position.position_id != str(existing["position_id"])
                    or historical_position.version != int(existing["position_version"])
                ):
                    raise AccountStoreConflict("funding source changed its durable facts")
                balances, _, _ = self._balances(connection, account_id)
                return FundingCommit(historical_position, balances, stored, True)
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
            transaction = self._load_journal_by_id(
                connection,
                str(row["transaction_id"]),
            )
            payload = json.loads(str(row["payload"]))
            position_payload = payload.get("position")
            if transaction is None or not isinstance(position_payload, dict):
                raise AccountStoreConflict("funding application lost its durable facts")
            position = PortfolioPosition.from_dict(position_payload)
            if (
                position.account_id != account_id
                or position.symbol != symbol
                or position.position_id != str(row["position_id"])
                or position.version != int(row["position_version"])
                or transaction.account_id != account_id
                or transaction.source_id != source_id
                or transaction.event_time != int(row["event_time"])
            ):
                raise AccountStoreConflict("funding application durable facts disagree")
            balances, _, _ = self._balances(connection, account_id)
        return FundingCommit(position, balances, transaction, True)
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
            transaction = existing.transaction
            try:
                stored_rate = as_decimal(transaction.evidence["rate"], "stored funding rate")
                stored_mark = as_decimal(
                    transaction.evidence["mark_price"],
                    "stored funding mark price",
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PortfolioSourceError("persisted funding evidence is invalid") from exc
            if (
                transaction.event_time != event_time
                or transaction.evidence.get("symbol") != symbol
                or stored_rate != as_decimal(rate, "funding rate")
                or stored_mark != as_decimal(mark_price, "funding mark price")
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

append_once(
    TEST,
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
''',
)
