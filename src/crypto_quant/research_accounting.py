from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .execution_contracts import ExecutionCommand, ExecutionOrder, OrderLegType, OrderUpdate
from .portfolio_accounting import (
    OrderAccountingCursor,
    PortfolioAccountingError,
    apply_close_fill,
    apply_funding_to_position,
    apply_mark_fact,
    apply_open_fill,
    initialize_account_transaction,
    plan_command_reservation,
    release_command_reservation,
)
from .portfolio_contracts import (
    ZERO,
    AccountBalances,
    AccountPolicy,
    CommandReservation,
    JournalTransaction,
    PortfolioPosition,
    ReservationState,
    as_decimal,
)
from .portfolio_facts import ClosedPosition, MarkPriceFact
from .risk_contracts import RiskDecision


class ResearchAccountConflict(RuntimeError):
    """Raised when one deterministic research fact is replayed with new semantics."""


@dataclass(slots=True)
class ResearchAccount:
    """In-memory research account using the exact Portfolio accounting functions.

    This adapter deliberately has no persistence, network client or independent cash/PnL
    formula. It exists to migrate backtests away from ``EngineState`` accounting while
    preserving fast deterministic research execution.
    """

    account_id: str
    policy: AccountPolicy = field(default_factory=AccountPolicy)
    balances: AccountBalances = field(default_factory=AccountBalances)
    positions: dict[str, PortfolioPosition] = field(default_factory=dict)
    reservations: dict[str, CommandReservation] = field(default_factory=dict)
    cursors: dict[str, OrderAccountingCursor] = field(default_factory=dict)
    journal: list[JournalTransaction] = field(default_factory=list)
    closed_positions: list[ClosedPosition] = field(default_factory=list)
    marks: dict[str, MarkPriceFact] = field(default_factory=dict)
    _transactions: dict[str, JournalTransaction] = field(default_factory=dict, repr=False)
    _updates: dict[str, OrderUpdate] = field(default_factory=dict, repr=False)
    _update_results: dict[str, PortfolioPosition | None] = field(
        default_factory=dict,
        repr=False,
    )
    _funding_sources: dict[str, tuple[str, Decimal, Decimal, int]] = field(
        default_factory=dict,
        repr=False,
    )
    _funding_results: dict[str, PortfolioPosition] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("research account_id must not be empty")

    @classmethod
    def initialized(
        cls,
        *,
        account_id: str,
        initial_capital: Decimal | int | float | str,
        event_time: int,
        source_id: str,
        policy: AccountPolicy | None = None,
    ) -> ResearchAccount:
        account = cls(account_id=account_id, policy=policy or AccountPolicy())
        transaction = initialize_account_transaction(
            account_id=account_id,
            initial_capital=initial_capital,
            event_time=event_time,
            source_id=source_id,
            policy=account.policy,
        )
        account._commit_transaction(transaction)
        return account

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((position.unrealized_pnl for position in self.positions.values()), ZERO)

    @property
    def equity(self) -> Decimal:
        return self.balances.settled_assets + self.unrealized_pnl

    def _commit_transaction(self, transaction: JournalTransaction) -> bool:
        existing = self._transactions.get(transaction.transaction_id)
        if existing is not None:
            if existing.semantic_dict() != transaction.semantic_dict():
                raise ResearchAccountConflict(
                    f"research journal identity collision: {transaction.transaction_id}"
                )
            return False
        if transaction.account_id != self.account_id:
            raise ResearchAccountConflict("research transaction belongs to another account")
        next_balances = self.balances.apply(transaction)
        self._transactions[transaction.transaction_id] = transaction
        self.journal.append(transaction)
        self.balances = next_balances
        return True

    def _duplicate_update(self, update: OrderUpdate) -> tuple[bool, PortfolioPosition | None]:
        existing = self._updates.get(update.update_id)
        if existing is None:
            return False, None
        if existing.to_dict() != update.to_dict():
            raise ResearchAccountConflict(
                f"research order update identity collision: {update.update_id}"
            )
        return True, self._update_results[update.update_id]

    def _record_update(
        self,
        update: OrderUpdate,
        result: PortfolioPosition | None,
    ) -> None:
        self._updates[update.update_id] = update
        self._update_results[update.update_id] = result

    def cursor_for(self, order: ExecutionOrder) -> OrderAccountingCursor:
        cursor = self.cursors.get(order.order_id)
        if cursor is None:
            cursor = OrderAccountingCursor(
                order_id=order.order_id,
                command_id=order.command_id,
            )
        elif cursor.command_id != order.command_id:
            raise ResearchAccountConflict("order cursor changed command identity")
        return cursor

    def reserve(
        self,
        *,
        command: ExecutionCommand,
        decision: RiskDecision,
        now_ms: int,
    ) -> CommandReservation:
        if command.account_id != self.account_id:
            raise PortfolioAccountingError("command belongs to another research account")
        existing = self.reservations.get(command.command_id)
        if existing is not None:
            expected = (
                self.account_id,
                command.source_decision_id,
                command.symbol,
                command.action,
                as_decimal(command.quantity, "command quantity"),
            )
            actual = (
                existing.account_id,
                existing.source_decision_id,
                existing.symbol,
                existing.action,
                existing.command_quantity,
            )
            if actual != expected or decision.decision_id != command.source_decision_id:
                raise ResearchAccountConflict(
                    f"research reservation changed: {command.command_id}"
                )
            return existing
        current_position = self.positions.get(command.symbol)
        locked_exit = sum(
            (
                reservation.reserved_exit_quantity
                for reservation in self.reservations.values()
                if reservation.symbol == command.symbol
                and reservation.state is ReservationState.ACTIVE
                and not reservation.entry
            ),
            ZERO,
        )
        plan = plan_command_reservation(
            account_id=self.account_id,
            command=command,
            decision=decision,
            balances=self.balances,
            policy=self.policy,
            now_ms=now_ms,
            current_position=current_position,
            already_locked_exit_quantity=locked_exit,
        )
        if plan.transaction is not None:
            self._commit_transaction(plan.transaction)
        self.reservations[command.command_id] = plan.reservation
        return plan.reservation

    def apply_open_update(
        self,
        *,
        command: ExecutionCommand,
        decision: RiskDecision,
        order: ExecutionOrder,
        update: OrderUpdate,
        maintenance_margin_rate: Decimal | int | float | str,
        correlation_group: str = "default",
    ) -> PortfolioPosition:
        duplicate, prior_result = self._duplicate_update(update)
        if duplicate:
            if prior_result is None:
                raise ResearchAccountConflict("open update previously produced no position")
            return prior_result
        reservation = self._reservation(command.command_id)
        result = apply_open_fill(
            command=command,
            decision=decision,
            order=order,
            update=update,
            cursor=self.cursor_for(order),
            reservation=reservation,
            balances=self.balances,
            policy=self.policy,
            maintenance_margin_rate=maintenance_margin_rate,
            correlation_group=correlation_group,
            position=self.positions.get(command.symbol),
        )
        self._commit_transaction(result.transaction)
        if result.position is None:
            raise ResearchAccountConflict("open fill produced no active position")
        self.reservations[command.command_id] = result.reservation
        self.positions[command.symbol] = result.position
        self.cursors[order.order_id] = result.cursor
        self._record_update(update, result.position)
        return result.position

    def apply_close_update(
        self,
        *,
        command: ExecutionCommand,
        order: ExecutionOrder,
        update: OrderUpdate,
    ) -> PortfolioPosition | None:
        duplicate, prior_result = self._duplicate_update(update)
        if duplicate:
            return prior_result
        reservation = self._reservation(command.command_id)
        position = self.positions.get(command.symbol)
        if position is None:
            raise PortfolioAccountingError("close update requires an active position")
        result = apply_close_fill(
            command=command,
            order=order,
            update=update,
            cursor=self.cursor_for(order),
            reservation=reservation,
            balances=self.balances,
            policy=self.policy,
            position=position,
        )
        self._commit_transaction(result.transaction)
        self.reservations[command.command_id] = result.reservation
        self.cursors[order.order_id] = result.cursor
        if result.position is None:
            del self.positions[command.symbol]
            if result.closed_position is None:
                raise ResearchAccountConflict("full close produced no closed-position fact")
            self.closed_positions.append(result.closed_position)
        else:
            self.positions[command.symbol] = result.position
        self._record_update(update, result.position)
        return result.position

    def apply_funding(
        self,
        *,
        symbol: str,
        rate: Decimal | int | float | str,
        mark_price: Decimal | int | float | str,
        event_time: int,
        source_id: str,
    ) -> PortfolioPosition:
        selected = (
            symbol,
            as_decimal(rate, "funding rate"),
            as_decimal(mark_price, "funding mark price"),
            event_time,
        )
        existing = self._funding_sources.get(source_id)
        if existing is not None:
            if existing != selected:
                raise ResearchAccountConflict(
                    f"research funding identity collision: {source_id}"
                )
            return self._funding_results[source_id]
        position = self.positions.get(symbol)
        if position is None:
            raise PortfolioAccountingError("funding requires an active position")
        result = apply_funding_to_position(
            account_id=self.account_id,
            position=position,
            rate=selected[1],
            mark_price=selected[2],
            event_time=event_time,
            source_id=source_id,
            policy=self.policy,
        )
        self._commit_transaction(result.transaction)
        self.positions[symbol] = result.position
        self._funding_sources[source_id] = selected
        self._funding_results[source_id] = result.position
        return result.position

    def apply_mark(self, fact: MarkPriceFact) -> PortfolioPosition | None:
        if fact.account_id != self.account_id:
            raise PortfolioAccountingError("mark fact belongs to another research account")
        existing = self.marks.get(fact.symbol)
        if existing is not None:
            if fact.observed_at < existing.observed_at:
                raise PortfolioAccountingError("research mark moved backwards")
            if fact.observed_at == existing.observed_at:
                if fact.semantic_dict() != existing.semantic_dict():
                    raise ResearchAccountConflict("same-time research mark was revised")
                return self.positions.get(fact.symbol)
        result = apply_mark_fact(self.positions.get(fact.symbol), fact)
        self.marks[fact.symbol] = fact
        if result.position is not None:
            self.positions[fact.symbol] = result.position
        return result.position

    def release(
        self,
        *,
        command_id: str,
        event_time: int,
        reason: str,
    ) -> CommandReservation:
        reservation = self._reservation(command_id)
        release = release_command_reservation(
            reservation=reservation,
            policy=self.policy,
            event_time=event_time,
            reason=reason,
        )
        if release.transaction is not None:
            self._commit_transaction(release.transaction)
        self.reservations[command_id] = release.reservation
        return release.reservation

    def _reservation(self, command_id: str) -> CommandReservation:
        try:
            return self.reservations[command_id]
        except KeyError as exc:
            raise PortfolioAccountingError(
                f"research command has no reservation: {command_id}"
            ) from exc

    def primary_order(self, command: ExecutionCommand) -> ExecutionOrder:
        orders = [order for order in command.orders if order.leg_type is OrderLegType.PRIMARY]
        if len(orders) != 1:
            raise PortfolioAccountingError("command requires exactly one primary order")
        return orders[0]
