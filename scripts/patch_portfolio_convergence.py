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
    STORE,
    '''            if instance.status is AccountStatus.HALTED:
                raise AccountInstanceError("halted account cannot reserve a command")
''',
    '''            if instance.status is AccountStatus.HALTED and reservation.entry:
                raise AccountInstanceError("halted account cannot reserve an entry command")
''',
)
