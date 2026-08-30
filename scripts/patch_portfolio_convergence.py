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
    file.write_text(text.rstrip() + "\n\n" + content.lstrip("\n"), encoding="utf-8")


ENGINE = "src/crypto_quant/portfolio_engine.py"
STORE = "src/crypto_quant/portfolio_store.py"
TEST = "tests/test_portfolio_engine.py"

replace_once(
    ENGINE,
    '''        self._assert_initialized()
        self.health.commands_received += 1
''',
    '''        self._assert_initialized(require_active=False)
        self.health.commands_received += 1
''',
)

replace_once(
    ENGINE,
    '''            command_event = self.store.load_execution_command_event_by_id(
                self.settings.account_id,
                exact.update.order_id,
            )
            command_id = self.execution_store.command_id_for_order(
''',
    '''            command_id = self.execution_store.command_id_for_order(
''',
)

replace_once(
    ENGINE,
    '''                    disposition.duplicate,
                )

            decision = self._risk_source(command)
            position = self.store.load_position(
''',
    '''                    disposition.duplicate,
                )

            instance = self.store.load_instance(self.settings.account_id)
            if instance is None:
                raise PortfolioHealthError("portfolio account has not been initialized")
            if (
                instance.status is AccountStatus.HALTED
                and command.action
                in {
                    RiskApprovedAction.OPEN_LONG,
                    RiskApprovedAction.OPEN_SHORT,
                }
            ):
                disposition = self.store.record_execution_command_disposition(
                    account_id=self.settings.account_id,
                    source_sequence=sequence,
                    command=command,
                    disposition=StoredExecutionDisposition.NON_EXECUTABLE,
                    processed_at=current,
                )
                self.health.commands_processed += 1
                self.health.last_error = None
                return CommandAccountingResult(
                    command.command_id,
                    sequence,
                    disposition.disposition,
                    None,
                    disposition.duplicate,
                )

            decision = self._risk_source(command)
            position = self.store.load_position(
''',
)

replace_once(
    ENGINE,
    '''                policy=self.policy,
                now_ms=current,
                current_position=position,
''',
    '''                policy=self.policy,
                now_ms=command.created_at,
                current_position=position,
''',
)

replace_once(
    ENGINE,
    '''            if command.delivery_state not in {
                ExecutionDeliveryState.RECORDED,
                ExecutionDeliveryState.COMPLETED,
                ExecutionDeliveryState.EXPIRED,
                ExecutionDeliveryState.FAILED,
            }:
                continue
            release = release_command_reservation(
                reservation=reservation,
                policy=self.policy,
                event_time=max(current, reservation.updated_at),
                reason=f"execution_{command.delivery_state.value}",
            )
''',
    '''            terminal_times = {
                ExecutionDeliveryState.COMPLETED: command.completed_at,
                ExecutionDeliveryState.EXPIRED: command.expired_at,
                ExecutionDeliveryState.FAILED: command.failed_at,
            }
            if command.delivery_state not in terminal_times:
                continue
            terminal_time = terminal_times[command.delivery_state]
            if terminal_time is None:
                raise PortfolioSourceError(
                    "execution terminal state is missing its durable timestamp"
                )
            if terminal_time < command.command.created_at:
                raise PortfolioSourceError(
                    "execution terminal time precedes command creation"
                )
            release = release_command_reservation(
                reservation=reservation,
                policy=self.policy,
                event_time=max(terminal_time, reservation.updated_at),
                reason=f"execution_{command.delivery_state.value}",
            )
''',
)

replace_once(
    STORE,
    '''            if instance.status is AccountStatus.HALTED:
                raise AccountInstanceError("halted account cannot reserve a command")
''',
    '''            if instance.status is AccountStatus.HALTED and reservation.entry:
                raise AccountInstanceError("halted account cannot reserve an entry command")
''',
)

append_once(
    TEST,
    "def test_delayed_command_processing_uses_durable_source_time(",
    '''def test_delayed_command_processing_uses_durable_source_time(tmp_path) -> None:
    from crypto_quant.portfolio_contracts import JournalSourceKind

    (
        engine,
        _,
        _,
        _,
        portfolio_store,
        _,
        stored_command,
    ) = system(tmp_path)

    result = engine.process_command(
        stored_command,
        processed_at=NOW + 10_000,
    )
    assert result.reservation is not None
    assert result.reservation.created_at == stored_command.command.created_at
    assert result.reservation.updated_at == stored_command.command.created_at

    reservations = [
        transaction
        for transaction in portfolio_store.journal_transactions("paper")
        if transaction.source_kind is JournalSourceKind.COMMAND_RESERVED
    ]
    assert len(reservations) == 1
    assert reservations[0].event_time == stored_command.command.created_at


def test_delayed_bootstrap_accounts_earlier_persisted_fills(tmp_path) -> None:
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

    result = engine.bootstrap(now_ms=NOW + 10_000)

    assert result.commands_processed == 1
    assert result.updates_processed == execution_store.latest_order_update_sequence()
    position = portfolio_store.load_position(
        "paper",
        stored_command.command.symbol,
    )
    assert position is not None
    assert position.opened_at == NOW + 2


def test_terminal_reservation_release_uses_execution_transition_time(tmp_path) -> None:
    from crypto_quant.portfolio_contracts import JournalSourceKind, ReservationState

    (
        engine,
        _,
        _,
        execution_store,
        portfolio_store,
        _,
        stored_command,
    ) = system(tmp_path)
    engine.process_command(stored_command, processed_at=NOW + 100)
    execution_store.mark_failed(
        stored_command.command.command_id,
        NOW + 2,
        "deterministic_release_test",
    )

    released = engine.release_terminal_reservations(now_ms=NOW + 20_000)

    assert released == 1
    reservation = portfolio_store.load_reservation(
        stored_command.command.command_id
    )
    assert reservation is not None
    assert reservation.state is ReservationState.RELEASED
    assert reservation.updated_at == NOW + 2
    releases = [
        transaction
        for transaction in portfolio_store.journal_transactions("paper")
        if transaction.source_kind is JournalSourceKind.COMMAND_RELEASED
    ]
    assert len(releases) == 1
    assert releases[0].event_time == NOW + 2
''',
)
