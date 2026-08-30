from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


ENGINE = "src/crypto_quant/portfolio_engine.py"
STORE = "src/crypto_quant/portfolio_store.py"
TEST = "tests/test_portfolio_engine.py"

replace_once(
    ENGINE,
    '''    def _assert_initialized(self) -> None:
        instance = self.store.load_instance(self.settings.account_id)
        if instance is None:
            raise PortfolioHealthError("portfolio account has not been initialized")
        if instance.status is AccountStatus.HALTED:
            raise PortfolioHealthError(
                f"portfolio account is halted: {instance.halt_reason}"
            )
        self.health.initialized = True
''',
    '''    def _assert_initialized(self, *, require_active: bool = True) -> None:
        instance = self.store.load_instance(self.settings.account_id)
        if instance is None:
            raise PortfolioHealthError("portfolio account has not been initialized")
        if require_active and instance.status is AccountStatus.HALTED:
            raise PortfolioHealthError(
                f"portfolio account is halted: {instance.halt_reason}"
            )
        self.health.initialized = True
''',
)
replace_once(
    ENGINE,
    '''        self._assert_initialized()
        self.health.updates_received += 1
''',
    '''        self._assert_initialized(require_active=False)
        self.health.updates_received += 1
''',
)
replace_once(
    ENGINE,
    '''        self._assert_initialized()
        fact = make_mark_price_fact(
''',
    '''        self._assert_initialized(require_active=False)
        fact = make_mark_price_fact(
''',
)
replace_once(
    ENGINE,
    '''        self._assert_initialized()
        position = self.store.load_position(self.settings.account_id, symbol)
''',
    '''        self._assert_initialized(require_active=False)
        position = self.store.load_position(self.settings.account_id, symbol)
''',
)
replace_once(
    ENGINE,
    '''        self._assert_initialized()
        commands_processed = 0
''',
    '''        self._assert_initialized(require_active=False)
        commands_processed = 0
''',
)
replace_once(
    ENGINE,
    '''        exact = rows[0]
        if exact.update.to_dict() != stored.update.to_dict():
''',
    '''        exact = rows[0]
        if exact.update.event_time > int(time.time() * 1000) + 300_000:
            raise PortfolioSourceError("order update is unreasonably far in the future")
        if exact.update.to_dict() != stored.update.to_dict():
''',
)

replace_once(
    STORE,
    '''        connection.execute(
            """INSERT INTO account_execution_command_events(
                account_id,source_sequence,command_id,disposition,processed_at,payload
            ) VALUES(?,?,?,?,?,?)""",
''',
    '''        latest = int(
            connection.execute(
                """SELECT coalesce(max(source_sequence),0)
                FROM account_execution_command_events WHERE account_id=?""",
                (account_id,),
            ).fetchone()[0]
        )
        if source_sequence <= latest:
            raise AccountStoreConflict("execution command sequence moved backwards")
        connection.execute(
            """INSERT INTO account_execution_command_events(
                account_id,source_sequence,command_id,disposition,processed_at,payload
            ) VALUES(?,?,?,?,?,?)""",
''',
)
replace_once(
    STORE,
    '''        connection.execute(
            """INSERT INTO account_execution_update_events(
                account_id,source_sequence,update_id,command_id,disposition,
                transaction_id,processed_at,payload
            ) VALUES(?,?,?,?,?,?,?,?)""",
''',
    '''        latest = int(
            connection.execute(
                """SELECT coalesce(max(source_sequence),0)
                FROM account_execution_update_events WHERE account_id=?""",
                (account_id,),
            ).fetchone()[0]
        )
        if source_sequence <= latest:
            raise AccountStoreConflict("execution update sequence moved backwards")
        connection.execute(
            """INSERT INTO account_execution_update_events(
                account_id,source_sequence,update_id,command_id,disposition,
                transaction_id,processed_at,payload
            ) VALUES(?,?,?,?,?,?,?,?)""",
''',
)
replace_once(
    STORE,
    '''            if instance.status is AccountStatus.HALTED:
                raise AccountInstanceError("halted account cannot accept fills")
            if self._load_journal_by_id(connection, transaction.transaction_id) is not None:
''',
    '''            if self._load_journal_by_id(connection, transaction.transaction_id) is not None:
''',
)

replace_once(
    TEST,
    '''from crypto_quant.portfolio_contracts import AccountHaltReason, AccountPolicy
''',
    '''from crypto_quant.portfolio_accounting import PortfolioAccountingError
from crypto_quant.portfolio_contracts import AccountHaltReason, AccountPolicy
''',
)
replace_once(
    TEST,
    '''    ExecutionContext,
    ExecutionMarketSnapshot,
''',
    '''    ExecutionCommandStatus,
    ExecutionContext,
    ExecutionMarketSnapshot,
''',
)
