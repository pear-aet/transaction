from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


def append_once(path: str, marker: str, content: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if marker in text:
        return
    file.write_text(text.rstrip() + "\n\n" + content.lstrip(), encoding="utf-8")


CONTRACTS = "src/crypto_quant/portfolio_contracts.py"
ACCOUNTING = "src/crypto_quant/portfolio_accounting.py"
STORE = "src/crypto_quant/portfolio_store.py"
EXECUTION_STORE = "src/crypto_quant/execution_store.py"

replace_once(
    CONTRACTS,
    '''    opened_at: int
    updated_at: int
    version: int = 1
''',
    '''    opened_at: int
    updated_at: int
    version: int = 1
    cumulative_closed_quantity: Decimal = ZERO
    cumulative_close_quote_value: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    opening_fees: Decimal = ZERO
    closing_fees: Decimal = ZERO
    funding_pnl: Decimal = ZERO
    last_funding_time: int | None = None
''',
)
replace_once(
    CONTRACTS,
    '''            "maintenance_margin_rate",
        ):
''',
    '''            "maintenance_margin_rate",
            "cumulative_closed_quantity",
            "cumulative_close_quote_value",
            "realized_pnl",
            "opening_fees",
            "closing_fees",
            "funding_pnl",
        ):
''',
)
replace_once(
    CONTRACTS,
    '''        if min(self.position_margin, self.initial_risk) < ZERO:
            raise ValueError("position margin and initial risk must be non-negative")
        if not ZERO < self.maintenance_margin_rate < Decimal("1"):
            raise ValueError("invalid maintenance_margin_rate")

    @property
    def signed_quantity(self) -> Decimal:
''',
    '''        if min(
            self.position_margin,
            self.initial_risk,
            self.cumulative_closed_quantity,
            self.cumulative_close_quote_value,
            self.opening_fees,
            self.closing_fees,
        ) < ZERO:
            raise ValueError("position monetary and cumulative values must be non-negative")
        if (
            self.cumulative_closed_quantity == ZERO
            and self.cumulative_close_quote_value != ZERO
        ):
            raise ValueError("position without closes cannot contain close quote value")
        if self.last_funding_time is not None:
            if self.last_funding_time < self.opened_at:
                raise ValueError("position funding time precedes opening")
            if self.last_funding_time > self.updated_at:
                raise ValueError("position funding time exceeds updated_at")
        if not ZERO < self.maintenance_margin_rate < Decimal("1"):
            raise ValueError("invalid maintenance_margin_rate")

    @property
    def total_opened_quantity(self) -> Decimal:
        return self.quantity + self.cumulative_closed_quantity

    @property
    def average_exit_price(self) -> Decimal | None:
        if self.cumulative_closed_quantity == ZERO:
            return None
        return self.cumulative_close_quote_value / self.cumulative_closed_quantity

    @property
    def net_realized_pnl(self) -> Decimal:
        return self.realized_pnl + self.funding_pnl - self.opening_fees - self.closing_fees

    @property
    def signed_quantity(self) -> Decimal:
''',
)
replace_once(
    CONTRACTS,
    '''            "updated_at": self.updated_at,
            "version": self.version,
        }
''',
    '''            "updated_at": self.updated_at,
            "version": self.version,
            "cumulative_closed_quantity": decimal_string(
                self.cumulative_closed_quantity
            ),
            "cumulative_close_quote_value": decimal_string(
                self.cumulative_close_quote_value
            ),
            "realized_pnl": decimal_string(self.realized_pnl),
            "opening_fees": decimal_string(self.opening_fees),
            "closing_fees": decimal_string(self.closing_fees),
            "funding_pnl": decimal_string(self.funding_pnl),
            "last_funding_time": self.last_funding_time,
        }
''',
)
replace_once(
    CONTRACTS,
    '''            updated_at=int(value["updated_at"]),
            version=int(value.get("version", 1)),
        )


@dataclass(frozen=True, slots=True)
class AccountWatermarks:
''',
    '''            updated_at=int(value["updated_at"]),
            version=int(value.get("version", 1)),
            cumulative_closed_quantity=as_decimal(
                value.get("cumulative_closed_quantity", "0"),
                "cumulative_closed_quantity",
            ),
            cumulative_close_quote_value=as_decimal(
                value.get("cumulative_close_quote_value", "0"),
                "cumulative_close_quote_value",
            ),
            realized_pnl=as_decimal(value.get("realized_pnl", "0"), "realized_pnl"),
            opening_fees=as_decimal(value.get("opening_fees", "0"), "opening_fees"),
            closing_fees=as_decimal(value.get("closing_fees", "0"), "closing_fees"),
            funding_pnl=as_decimal(value.get("funding_pnl", "0"), "funding_pnl"),
            last_funding_time=(
                None
                if value.get("last_funding_time") is None
                else int(value["last_funding_time"])
            ),
        )


@dataclass(frozen=True, slots=True)
class AccountWatermarks:
''',
)

replace_once(
    ACCOUNTING,
    '''from .portfolio_contracts import (
''',
    '''from .portfolio_facts import (
    ClosedPosition,
    MarkPriceFact,
    closed_position_id,
)
from .portfolio_contracts import (
''',
)
replace_once(
    ACCOUNTING,
    '''class FillAccountingResult:
    reservation: CommandReservation
    position: PortfolioPosition | None
    cursor: OrderAccountingCursor
    transaction: JournalTransaction
    realized_pnl: Decimal = ZERO
    position_closed: bool = False


@dataclass(frozen=True, slots=True)
class ReservationRelease:
''',
    '''class FillAccountingResult:
    reservation: CommandReservation
    position: PortfolioPosition | None
    cursor: OrderAccountingCursor
    transaction: JournalTransaction
    realized_pnl: Decimal = ZERO
    position_closed: bool = False
    closed_position: ClosedPosition | None = None


@dataclass(frozen=True, slots=True)
class FundingAccountingResult:
    position: PortfolioPosition
    transaction: JournalTransaction
    signed_funding_pnl: Decimal


@dataclass(frozen=True, slots=True)
class MarkAccountingResult:
    fact: MarkPriceFact
    position: PortfolioPosition | None


@dataclass(frozen=True, slots=True)
class ReservationRelease:
''',
)
replace_once(
    ACCOUNTING,
    '''            opened_at=update.event_time,
            updated_at=update.event_time,
        )
''',
    '''            opened_at=update.event_time,
            updated_at=update.event_time,
            opening_fees=delta.fee,
        )
''',
)
replace_once(
    ACCOUNTING,
    '''            initial_risk=position.initial_risk + initial_risk_delta,
            maintenance_margin_rate=margin_rate,
            updated_at=update.event_time,
''',
    '''            initial_risk=position.initial_risk + initial_risk_delta,
            maintenance_margin_rate=margin_rate,
            opening_fees=position.opening_fees + delta.fee,
            updated_at=update.event_time,
''',
)
replace_once(
    ACCOUNTING,
    '''    next_position = None
    if not full_close:
        next_position = replace(
            position,
            quantity=position.quantity - delta.quantity,
            mark_price=delta.price,
            position_margin=position.position_margin - margin_release,
            initial_risk=position.initial_risk - risk_release,
            updated_at=update.event_time,
            version=position.version + 1,
        )
''',
    '''    closed_quantity = position.cumulative_closed_quantity + delta.quantity
    closed_quote_value = position.cumulative_close_quote_value + delta.quote_value
    realized_after = position.realized_pnl + realized
    closing_fees_after = position.closing_fees + delta.fee
    next_position = None
    closed_position = None
    if not full_close:
        next_position = replace(
            position,
            quantity=position.quantity - delta.quantity,
            mark_price=delta.price,
            position_margin=position.position_margin - margin_release,
            initial_risk=position.initial_risk - risk_release,
            cumulative_closed_quantity=closed_quantity,
            cumulative_close_quote_value=closed_quote_value,
            realized_pnl=realized_after,
            closing_fees=closing_fees_after,
            updated_at=update.event_time,
            version=position.version + 1,
        )
    else:
        total_quantity = position.quantity + position.cumulative_closed_quantity
        if closed_quantity != total_quantity:
            raise PortfolioAccountingError("closed quantity does not match opened quantity")
        closed_position = ClosedPosition(
            closed_position_id=closed_position_id(
                position_id=position.position_id,
                close_command_id=command.command_id,
                close_update_id=update.update_id,
                closed_at=update.event_time,
            ),
            position_id=position.position_id,
            account_id=position.account_id,
            symbol=position.symbol,
            side=position.side,
            total_quantity=total_quantity,
            average_entry_price=position.average_entry_price,
            average_exit_price=closed_quote_value / closed_quantity,
            realized_pnl=realized_after,
            opening_fees=position.opening_fees,
            closing_fees=closing_fees_after,
            funding_pnl=position.funding_pnl,
            open_command_id=position.open_command_id,
            open_risk_decision_id=position.open_risk_decision_id,
            open_intent_id=position.open_intent_id,
            close_command_id=command.command_id,
            close_update_id=update.update_id,
            opened_at=position.opened_at,
            closed_at=update.event_time,
        )
''',
)
replace_once(
    ACCOUNTING,
    '''        realized_pnl=realized,
        position_closed=full_close,
    )


def release_command_reservation(
''',
    '''        realized_pnl=realized,
        position_closed=full_close,
        closed_position=closed_position,
    )


def release_command_reservation(
''',
)
append_once(
    ACCOUNTING,
    "def apply_funding_to_position(",
    '''def apply_funding_to_position(
    *,
    account_id: str,
    position: PortfolioPosition,
    rate: Decimal | int | float | str,
    mark_price: Decimal | int | float | str,
    event_time: int,
    source_id: str,
    policy: AccountPolicy,
) -> FundingAccountingResult:
    if account_id != position.account_id:
        raise PortfolioAccountingError("funding position belongs to another account")
    if event_time < position.updated_at:
        raise PortfolioAccountingError("funding event time moved backwards")
    if position.last_funding_time is not None and event_time <= position.last_funding_time:
        raise PortfolioAccountingError("funding event time did not advance")
    selected_rate = as_decimal(rate, "funding rate")
    selected_mark = as_decimal(mark_price, "funding mark price")
    sign = Decimal("1") if position.side is PositionSide.LONG else Decimal("-1")
    payment = policy.quantize_money(
        sign * position.quantity * selected_mark * selected_rate
    )
    signed_pnl = -payment
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
    return FundingAccountingResult(next_position, transaction, signed_pnl)


def apply_mark_fact(
    position: PortfolioPosition | None,
    fact: MarkPriceFact,
) -> MarkAccountingResult:
    if position is None:
        return MarkAccountingResult(fact, None)
    if position.account_id != fact.account_id or position.symbol != fact.symbol:
        raise PortfolioAccountingError("mark fact belongs to another position")
    next_position = apply_mark_price(
        position,
        mark_price=fact.mark_price,
        observed_at=fact.observed_at,
    )
    return MarkAccountingResult(fact, next_position)
''',
)

replace_once(
    STORE,
    '''    FillAccountingResult,
    OrderAccountingCursor,
''',
    '''    FillAccountingResult,
    FundingAccountingResult,
    MarkAccountingResult,
    OrderAccountingCursor,
''',
)
replace_once(
    STORE,
    '''from .portfolio_contracts import (
''',
    '''from .portfolio_facts import (
    ClosedPosition,
    MarkPriceFact,
    ReconciliationFinding,
    ReconciliationSeverity,
)
from .portfolio_contracts import (
''',
)
replace_once(
    STORE,
    '''class FillCommit:
    reservation: CommandReservation
    position: PortfolioPosition | None
    cursor: OrderAccountingCursor
    balances: AccountBalances
    transaction: JournalTransaction
    duplicate: bool


@dataclass(frozen=True, slots=True)
class SnapshotCommit:
''',
    '''class FillCommit:
    reservation: CommandReservation
    position: PortfolioPosition | None
    closed_position: ClosedPosition | None
    cursor: OrderAccountingCursor
    balances: AccountBalances
    transaction: JournalTransaction
    duplicate: bool


@dataclass(frozen=True, slots=True)
class FundingCommit:
    position: PortfolioPosition
    balances: AccountBalances
    transaction: JournalTransaction
    duplicate: bool


@dataclass(frozen=True, slots=True)
class MarkCommit:
    fact: MarkPriceFact
    position: PortfolioPosition | None
    duplicate: bool


@dataclass(frozen=True, slots=True)
class FindingCommit:
    finding: ReconciliationFinding
    duplicate: bool


@dataclass(frozen=True, slots=True)
class SnapshotCommit:
''',
)
replace_once(
    STORE,
    '''                CREATE TABLE IF NOT EXISTS account_order_cursors (
''',
    '''                CREATE TABLE IF NOT EXISTS account_closed_positions (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    closed_position_id TEXT NOT NULL UNIQUE,
                    position_id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    close_update_id TEXT NOT NULL UNIQUE,
                    closed_at INTEGER NOT NULL CHECK(closed_at >= 0),
                    payload TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );
                CREATE INDEX IF NOT EXISTS account_closed_positions_account_sequence
                    ON account_closed_positions(account_id, storage_sequence);

                CREATE TABLE IF NOT EXISTS account_order_cursors (
''',
)
replace_once(
    STORE,
    '''                CREATE TABLE IF NOT EXISTS account_funding_applications (
                    source_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_time INTEGER NOT NULL CHECK(event_time >= 0),
                    transaction_id TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id),
                    FOREIGN KEY(transaction_id)
                        REFERENCES account_journal(transaction_id)
                );

                CREATE TABLE IF NOT EXISTS account_mark_prices (
''',
    '''                CREATE TABLE IF NOT EXISTS account_funding_applications (
                    source_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    position_version INTEGER NOT NULL CHECK(position_version > 0),
                    event_time INTEGER NOT NULL CHECK(event_time >= 0),
                    transaction_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id),
                    FOREIGN KEY(transaction_id)
                        REFERENCES account_journal(transaction_id)
                );

                CREATE TABLE IF NOT EXISTS account_mark_history (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    observed_at INTEGER NOT NULL CHECK(observed_at >= 0),
                    source_sequence INTEGER NOT NULL CHECK(source_sequence >= 0),
                    payload TEXT NOT NULL,
                    UNIQUE(account_id, source_event_id),
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );
                CREATE INDEX IF NOT EXISTS account_mark_history_account_sequence
                    ON account_mark_history(account_id, storage_sequence);

                CREATE TABLE IF NOT EXISTS account_mark_prices (
''',
)
replace_once(
    STORE,
    '''                CREATE TABLE IF NOT EXISTS account_mark_prices (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    observed_at INTEGER NOT NULL CHECK(observed_at >= 0),
                    source_sequence INTEGER NOT NULL CHECK(source_sequence >= 0),
                    mark_price TEXT NOT NULL,
                    PRIMARY KEY(account_id, symbol),
                    UNIQUE(account_id, source_event_id),
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );
                """
''',
    '''                CREATE TABLE IF NOT EXISTS account_mark_prices (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    fact_id TEXT NOT NULL UNIQUE,
                    source_event_id TEXT NOT NULL,
                    observed_at INTEGER NOT NULL CHECK(observed_at >= 0),
                    source_sequence INTEGER NOT NULL CHECK(source_sequence >= 0),
                    mark_price TEXT NOT NULL,
                    PRIMARY KEY(account_id, symbol),
                    UNIQUE(account_id, source_event_id),
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );

                CREATE TABLE IF NOT EXISTS account_reconciliation_findings (
                    storage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    finding_id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK(severity IN ('warning','critical')),
                    source_id TEXT NOT NULL,
                    found_at INTEGER NOT NULL CHECK(found_at >= 0),
                    payload TEXT NOT NULL,
                    UNIQUE(account_id, kind, source_id, found_at),
                    FOREIGN KEY(account_id) REFERENCES account_instances(account_id)
                );
                CREATE INDEX IF NOT EXISTS account_findings_account_sequence
                    ON account_reconciliation_findings(account_id, storage_sequence);
                """
''',
)
replace_once(
    STORE,
    '''    @staticmethod
    def _encode_cursor(cursor: OrderAccountingCursor) -> str:
''',
    '''    @staticmethod
    def _encode_closed_position(value: ClosedPosition) -> str:
        payload = value.to_dict()
        payload["storage_sequence"] = None
        return PortfolioStore._encode(payload)

    @staticmethod
    def _decode_closed_position(payload: str, sequence: int) -> ClosedPosition:
        return ClosedPosition.from_dict(json.loads(payload)).with_storage_sequence(sequence)

    @staticmethod
    def _encode_mark_fact(value: MarkPriceFact) -> str:
        payload = value.to_dict()
        payload["storage_sequence"] = None
        return PortfolioStore._encode(payload)

    @staticmethod
    def _decode_mark_fact(payload: str, sequence: int) -> MarkPriceFact:
        return MarkPriceFact.from_dict(json.loads(payload)).with_storage_sequence(sequence)

    @staticmethod
    def _encode_finding(value: ReconciliationFinding) -> str:
        payload = value.to_dict()
        payload["storage_sequence"] = None
        return PortfolioStore._encode(payload)

    @staticmethod
    def _decode_finding(payload: str, sequence: int) -> ReconciliationFinding:
        return ReconciliationFinding.from_dict(json.loads(payload)).with_storage_sequence(sequence)

    @staticmethod
    def _encode_cursor(cursor: OrderAccountingCursor) -> str:
''',
)
replace_once(
    STORE,
    '''                return FillCommit(
                    current_reservation,
                    position,
                    cursor,
                    balances,
                    existing_journal,
                    True,
                )
''',
    '''                closed_position = None
                closed_row = connection.execute(
                    """SELECT * FROM account_closed_positions
                    WHERE close_update_id=?""",
                    (transaction.source_id,),
                ).fetchone()
                if closed_row is not None:
                    closed_position = self._decode_closed_position(
                        str(closed_row["payload"]),
                        int(closed_row["storage_sequence"]),
                    )
                return FillCommit(
                    current_reservation,
                    position,
                    closed_position,
                    cursor,
                    balances,
                    existing_journal,
                    True,
                )
''',
)
replace_once(
    STORE,
    '''            current_position = self._stored_position(
                connection,
                reservation.account_id,
                reservation.symbol,
            )
            if result.position is None:
                if current_position is None:
                    raise AccountStoreConflict("fill attempted to close a missing position")
            elif current_position is None:
''',
    '''            current_position = self._stored_position(
                connection,
                reservation.account_id,
                reservation.symbol,
            )
            if result.position is None:
                if current_position is None:
                    raise AccountStoreConflict("fill attempted to close a missing position")
                if result.closed_position is None:
                    raise AccountStoreConflict("full close is missing its closed-position fact")
                if result.closed_position.position_id != current_position.position_id:
                    raise AccountStoreConflict("closed-position fact belongs to another position")
                if result.closed_position.close_update_id != transaction.source_id:
                    raise AccountStoreConflict("closed-position fact has another close update")
            elif result.closed_position is not None:
                raise AccountStoreConflict("active position fill cannot include a closed-position fact")
            elif current_position is None:
''',
)
replace_once(
    STORE,
    '''            if result.position is None:
                connection.execute(
                    "DELETE FROM account_positions WHERE account_id=? AND symbol=?",
                    (reservation.account_id, reservation.symbol),
                )
            else:
''',
    '''            stored_closed_position = None
            if result.position is None:
                connection.execute(
                    "DELETE FROM account_positions WHERE account_id=? AND symbol=?",
                    (reservation.account_id, reservation.symbol),
                )
                assert result.closed_position is not None
                cursor = connection.execute(
                    """INSERT INTO account_closed_positions(
                        closed_position_id,position_id,account_id,symbol,
                        close_update_id,closed_at,payload
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        result.closed_position.closed_position_id,
                        result.closed_position.position_id,
                        result.closed_position.account_id,
                        result.closed_position.symbol,
                        result.closed_position.close_update_id,
                        result.closed_position.closed_at,
                        self._encode_closed_position(result.closed_position),
                    ),
                )
                stored_closed_position = result.closed_position.with_storage_sequence(
                    int(cursor.lastrowid)
                )
            else:
''',
)
replace_once(
    STORE,
    '''        return FillCommit(
            reservation,
            result.position,
            result.cursor,
            balances,
            stored_transaction,
            False,
        )
''',
    '''        return FillCommit(
            reservation,
            result.position,
            stored_closed_position,
            result.cursor,
            balances,
            stored_transaction,
            False,
        )
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
        transaction: JournalTransaction,
        policy: AccountPolicy,
    ) -> StoredJournalTransaction:
        if transaction.source_kind is not JournalSourceKind.FUNDING:
            raise ValueError("funding transaction has the wrong source kind")
        if transaction.account_id != account_id or transaction.source_id != source_id:
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
                stored = self._load_journal_by_id(
                    connection,
                    str(existing["transaction_id"]),
                )
                if stored is None or not self._same_transaction(stored, transaction):
                    raise AccountStoreConflict("funding source changed its transaction")
                return StoredJournalTransaction(stored, True)
            stored, _, duplicate = self._commit_transaction(connection, transaction)
            if duplicate:
                raise AccountStoreConflict("funding journal exists without application row")
            connection.execute(
                """INSERT INTO account_funding_applications(
                    source_id,account_id,symbol,event_time,transaction_id
                ) VALUES(?,?,?,?,?)""",
                (
                    source_id,
                    account_id,
                    symbol,
                    transaction.event_time,
                    transaction.transaction_id,
                ),
            )
            connection.execute(
                "UPDATE account_instances SET updated_at=? WHERE account_id=?",
                (transaction.event_time, account_id),
            )
        return StoredJournalTransaction(stored, False)
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
)
append_once(
    STORE,
    "def commit_mark(",
    '''def commit_mark(
        self,
        result: MarkAccountingResult,
        policy: AccountPolicy,
    ) -> MarkCommit:
        fact = result.fact
        if result.position is not None:
            if result.position.account_id != fact.account_id or result.position.symbol != fact.symbol:
                raise ValueError("mark result belongs to another position")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            instance_row = connection.execute(
                "SELECT * FROM account_instances WHERE account_id=?",
                (fact.account_id,),
            ).fetchone()
            if instance_row is None:
                raise KeyError(f"account not found: {fact.account_id}")
            instance = self._instance_from_row(instance_row)
            self._assert_instance(instance, policy)
            existing_row = connection.execute(
                "SELECT * FROM account_mark_history WHERE fact_id=?",
                (fact.fact_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._decode_mark_fact(
                    str(existing_row["payload"]),
                    int(existing_row["storage_sequence"]),
                )
                if existing.semantic_dict() != fact.semantic_dict():
                    raise AccountStoreConflict("mark fact ID collision")
                current = self._stored_position(connection, fact.account_id, fact.symbol)
                return MarkCommit(existing, current, True)
            latest = connection.execute(
                """SELECT * FROM account_mark_prices
                WHERE account_id=? AND symbol=?""",
                (fact.account_id, fact.symbol),
            ).fetchone()
            if latest is not None:
                latest_time = int(latest["observed_at"])
                latest_sequence = int(latest["source_sequence"])
                latest_price = str(latest["mark_price"])
                if fact.observed_at < latest_time or fact.source_sequence < latest_sequence:
                    raise AccountStoreConflict("mark fact moved backwards")
                if fact.observed_at == latest_time:
                    if str(fact.mark_price) != latest_price:
                        raise AccountStoreConflict("same-time mark price revision")
                    raise AccountStoreConflict("same-time mark fact changed source identity")
            current = self._stored_position(connection, fact.account_id, fact.symbol)
            if current is None:
                if result.position is not None:
                    raise AccountStoreConflict("mark result created an unstored position")
            else:
                if result.position is None:
                    raise AccountStoreConflict("mark result omitted an active position")
                if result.position.position_id != current.position_id:
                    raise AccountStoreConflict("mark position identity changed")
                if result.position.version != current.version + 1:
                    raise AccountStoreConflict("stale or skipped mark position version")
            cursor = connection.execute(
                """INSERT INTO account_mark_history(
                    fact_id,account_id,source_event_id,symbol,observed_at,
                    source_sequence,payload
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    fact.fact_id,
                    fact.account_id,
                    fact.source_event_id,
                    fact.symbol,
                    fact.observed_at,
                    fact.source_sequence,
                    self._encode_mark_fact(fact),
                ),
            )
            stored_fact = fact.with_storage_sequence(int(cursor.lastrowid))
            connection.execute(
                """INSERT INTO account_mark_prices(
                    account_id,symbol,fact_id,source_event_id,observed_at,
                    source_sequence,mark_price
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(account_id,symbol) DO UPDATE SET
                    fact_id=excluded.fact_id,
                    source_event_id=excluded.source_event_id,
                    observed_at=excluded.observed_at,
                    source_sequence=excluded.source_sequence,
                    mark_price=excluded.mark_price""",
                (
                    fact.account_id,
                    fact.symbol,
                    fact.fact_id,
                    fact.source_event_id,
                    fact.observed_at,
                    fact.source_sequence,
                    str(fact.mark_price),
                ),
            )
            if result.position is not None:
                connection.execute(
                    """UPDATE account_positions SET
                        version=?,updated_at=?,payload=?
                        WHERE account_id=? AND symbol=?""",
                    (
                        result.position.version,
                        result.position.updated_at,
                        self._encode_position(result.position),
                        fact.account_id,
                        fact.symbol,
                    ),
                )
            connection.execute(
                "UPDATE account_instances SET updated_at=? WHERE account_id=?",
                (fact.observed_at, fact.account_id),
            )
        return MarkCommit(stored_fact, result.position, False)

    def latest_mark(self, account_id: str, symbol: str) -> MarkPriceFact | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT h.* FROM account_mark_history h
                JOIN account_mark_prices p ON p.fact_id=h.fact_id
                WHERE p.account_id=? AND p.symbol=?""",
                (account_id, symbol),
            ).fetchone()
        if row is None:
            return None
        return self._decode_mark_fact(str(row["payload"]), int(row["storage_sequence"]))

    def load_closed_position(self, closed_position_id: str) -> ClosedPosition | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM account_closed_positions
                WHERE closed_position_id=?""",
                (closed_position_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_closed_position(
            str(row["payload"]),
            int(row["storage_sequence"]),
        )

    def list_closed_positions(
        self,
        account_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[ClosedPosition]:
        if after_sequence < 0 or not 1 <= limit <= 10_000:
            raise ValueError("invalid closed-position range")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM account_closed_positions
                WHERE account_id=? AND storage_sequence>?
                ORDER BY storage_sequence LIMIT ?""",
                (account_id, after_sequence, limit),
            ).fetchall()
        return [
            self._decode_closed_position(str(row["payload"]), int(row["storage_sequence"]))
            for row in rows
        ]

    def commit_finding(self, finding: ReconciliationFinding) -> FindingCommit:
        if finding.storage_sequence is not None:
            raise ValueError("only unstored reconciliation findings may be committed")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM account_reconciliation_findings
                WHERE finding_id=?""",
                (finding.finding_id,),
            ).fetchone()
            if row is not None:
                existing = self._decode_finding(
                    str(row["payload"]),
                    int(row["storage_sequence"]),
                )
                if existing.semantic_dict() != finding.semantic_dict():
                    raise AccountStoreConflict("reconciliation finding ID collision")
                return FindingCommit(existing, True)
            cursor = connection.execute(
                """INSERT INTO account_reconciliation_findings(
                    finding_id,account_id,kind,severity,source_id,found_at,payload
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    finding.finding_id,
                    finding.account_id,
                    finding.kind.value,
                    finding.severity.value,
                    finding.source_id,
                    finding.found_at,
                    self._encode_finding(finding),
                ),
            )
            stored = finding.with_storage_sequence(int(cursor.lastrowid))
            if finding.severity is ReconciliationSeverity.CRITICAL:
                connection.execute(
                    """UPDATE account_instances SET
                    status='halted',halt_reason=?,updated_at=? WHERE account_id=?""",
                    (
                        f"reconciliation:{finding.kind.value}:{finding.finding_id}",
                        finding.found_at,
                        finding.account_id,
                    ),
                )
        return FindingCommit(stored, False)

    def reconciliation_findings(
        self,
        account_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[ReconciliationFinding]:
        if after_sequence < 0 or not 1 <= limit <= 10_000:
            raise ValueError("invalid reconciliation range")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM account_reconciliation_findings
                WHERE account_id=? AND storage_sequence>?
                ORDER BY storage_sequence LIMIT ?""",
                (account_id, after_sequence, limit),
            ).fetchall()
        return [
            self._decode_finding(str(row["payload"]), int(row["storage_sequence"]))
            for row in rows
        ]
''',
)

append_once(
    EXECUTION_STORE,
    "def load_commands(",
    '''def load_commands(
        self,
        *,
        after_sequence: int = 0,
        through_sequence: int | None = None,
        engine_instance_id: str | None = None,
        account_id: str | None = None,
        limit: int = 1000,
    ) -> list[StoredExecutionCommand]:
        if after_sequence < 0 or not 1 <= limit <= 10_000:
            raise ValueError("invalid command scan range")
        if through_sequence is not None and through_sequence < after_sequence:
            return []
        query = """SELECT * FROM execution_commands
        WHERE storage_sequence>?
        AND (? IS NULL OR storage_sequence<=?)
        AND (? IS NULL OR engine_instance_id=?)
        AND (? IS NULL OR account_id=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (
            after_sequence,
            through_sequence,
            through_sequence,
            engine_instance_id,
            engine_instance_id,
            account_id,
            account_id,
            limit,
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._stored_from_row(connection, row) for row in rows]

    def latest_order_update_sequence(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT coalesce(max(storage_sequence),0) FROM execution_order_updates"
            ).fetchone()
        return int(row[0])

    def load_order_updates(
        self,
        *,
        after_sequence: int = 0,
        through_sequence: int | None = None,
        account_id: str | None = None,
        command_id: str | None = None,
        limit: int = 1000,
    ) -> list[StoredOrderUpdate]:
        if after_sequence < 0 or not 1 <= limit <= 10_000:
            raise ValueError("invalid order-update scan range")
        if through_sequence is not None and through_sequence < after_sequence:
            return []
        query = """SELECT u.* FROM execution_order_updates u
        JOIN execution_commands c ON c.command_id=u.command_id
        WHERE u.storage_sequence>?
        AND (? IS NULL OR u.storage_sequence<=?)
        AND (? IS NULL OR c.account_id=?)
        AND (? IS NULL OR u.command_id=?)
        ORDER BY u.storage_sequence LIMIT ?"""
        parameters = (
            after_sequence,
            through_sequence,
            through_sequence,
            account_id,
            account_id,
            command_id,
            command_id,
            limit,
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            StoredOrderUpdate(
                update=self._decode_update(str(row["payload"])),
                storage_sequence=int(row["storage_sequence"]),
                prior_state=OrderState(row["prior_state"]),
                new_state=OrderState(row["new_state"]),
            )
            for row in rows
        ]
''',
)
