from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/crypto_quant/execution_reconcile.py",
    "    observed_by_client = {order.client_order_id: item for item in observed}\n",
    "    observed_by_client = {item.client_order_id: item for item in observed}\n",
)
replace_once(
    "src/crypto_quant/execution_reconcile.py",
    '''                        f"persisted {order.state.value} order is absent from complete broker ledger",
''',
    '''                        (
                            f"persisted {order.state.value} order is absent from "
                            "complete broker ledger"
                        ),
''',
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''from .execution_store import (
    ExecutionDeliveryState,
    ExecutionStore,
    StoredExecutionCommand,
)
''',
    '''from .execution_reconcile import (
    ExecutionReconciliationError,
    ExecutionReconciliationReport,
    ObservedExecutionOrder,
    reconcile_execution_orders,
    require_complete_reconciliation,
)
from .execution_store import (
    ExecutionDeliveryState,
    ExecutionStore,
    StoredExecutionCommand,
)
''',
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''    protection_activations: int = 0
    failures: int = 0
''',
    '''    protection_activations: int = 0
    reconciliation_runs: int = 0
    reconciliation_updates: int = 0
    reconciliation_failures: int = 0
    failures: int = 0
''',
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''            "protection_activations": self.protection_activations,
            "failures": self.failures,
''',
    '''            "protection_activations": self.protection_activations,
            "reconciliation_runs": self.reconciliation_runs,
            "reconciliation_updates": self.reconciliation_updates,
            "reconciliation_failures": self.reconciliation_failures,
            "failures": self.failures,
''',
)
replace_once(
    "src/crypto_quant/execution_engine.py",
    '''    async def health_snapshot(self) -> dict[str, Any]:
''',
    '''    async def reconcile_broker_ledger(
        self,
        command_id: str,
        observed_orders: tuple[ObservedExecutionOrder, ...],
        *,
        observed_at: int,
    ) -> ExecutionReconciliationReport:
        if not command_id or observed_at < 0:
            raise ValueError("invalid reconciliation identity or time")
        async with self._processing_lock:
            self.health.reconciliation_runs += 1
            self.health.last_command_id = command_id
            try:
                stored = await asyncio.to_thread(self.store.load_command, command_id)
                if stored is None:
                    raise KeyError(f"execution command not found: {command_id}")
                if stored.command.engine_instance_id != self.settings.engine_instance_id:
                    raise ExecutionSourceError(
                        "reconciliation command belongs to another execution instance"
                    )
                if stored.command.mode is not ExecutionMode.SIMULATION:
                    raise ExecutionDispatchError("shadow command cannot be reconciled as broker state")
                if stored.delivery_state is not ExecutionDeliveryState.DISPATCHED:
                    raise ExecutionDispatchError(
                        "broker reconciliation requires a dispatched simulation command"
                    )

                report = await asyncio.to_thread(
                    reconcile_execution_orders,
                    command_id,
                    stored.orders,
                    observed_orders,
                    observed_at=observed_at,
                )
                try:
                    require_complete_reconciliation(report)
                except ExecutionReconciliationError:
                    reason = "reconciliation:" + ",".join(
                        sorted({issue.kind.value for issue in report.issues})
                    )
                    changed = await asyncio.to_thread(
                        self.store.mark_failed,
                        command_id,
                        observed_at,
                        reason,
                    )
                    if changed:
                        self.health.commands_failed += 1
                    self.health.reconciliation_failures += 1
                    raise

                for update in report.updates:
                    commit = await asyncio.to_thread(self.store.apply_order_update, update)
                    if not commit.duplicate:
                        self.health.order_updates += 1
                        self.health.reconciliation_updates += 1

                refreshed = await asyncio.to_thread(self.store.load_command, command_id)
                if refreshed is None:
                    raise RuntimeError("execution command disappeared during reconciliation")
                primary = next(
                    (
                        order
                        for order in refreshed.orders
                        if order.leg_type is OrderLegType.PRIMARY
                    ),
                    None,
                )
                if (
                    primary is not None
                    and primary.state in {OrderState.PARTIALLY_FILLED, OrderState.FILLED}
                    and self.broker is not None
                ):
                    activation = await asyncio.to_thread(
                        self.broker.activate_protection,
                        refreshed.command,
                        refreshed.orders,
                        observed_at,
                    )
                    applied = await self._apply_updates(activation)
                    if applied:
                        self.health.protection_activations += 1
                        refreshed = await asyncio.to_thread(
                            self.store.load_command,
                            command_id,
                        )
                        if refreshed is None:
                            raise RuntimeError(
                                "execution command disappeared after reconciliation protection"
                            )
                await self._reconcile_command(refreshed, observed_at)
                self.health.last_error = None
                return report
            except Exception as exc:
                self._record_failure(exc)
                raise

    async def health_snapshot(self) -> dict[str, Any]:
''',
)
