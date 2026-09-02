from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from .execution_contracts import (
    ExecutionCommand,
    ExecutionMode,
    ExecutionOrder,
    OrderLegType,
    OrderUpdate,
)
from .portfolio_contracts import (
    ZERO,
    AccountBalances,
    AccountPolicy,
    AccountSnapshot,
    AccountWatermarks,
    CommandReservation,
    JournalAccount,
    JournalPosting,
    JournalSourceKind,
    JournalTransaction,
    PortfolioPosition,
    PositionSide,
    PostingSide,
    ReservationState,
    account_snapshot_id,
    as_decimal,
    decimal_string,
    make_journal_transaction,
)
from .risk_contracts import RiskApprovedAction, RiskDecision, RiskDecisionStatus


class PortfolioAccountingError(RuntimeError):
    """Raised when an execution fact would violate account invariants."""


@dataclass(frozen=True, slots=True)
class OrderAccountingCursor:
    order_id: str
    command_id: str
    cumulative_filled_quantity: Decimal = ZERO
    cumulative_quote_value: Decimal = ZERO
    cumulative_fee: Decimal = ZERO
    last_update_id: str | None = None
    updated_at: int = 0
    version: int = 0

    def __post_init__(self) -> None:
        if not self.order_id or not self.command_id:
            raise ValueError("order accounting identifiers must not be empty")
        if min(self.updated_at, self.version) < 0:
            raise ValueError("order accounting counters must be non-negative")
        for name in (
            "cumulative_filled_quantity",
            "cumulative_quote_value",
            "cumulative_fee",
        ):
            value = as_decimal(getattr(self, name), name)
            if value < ZERO:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        if self.cumulative_filled_quantity == ZERO and self.cumulative_quote_value != ZERO:
            raise ValueError("unfilled accounting cursor cannot contain quote value")
        if self.version == 0 and self.last_update_id is not None:
            raise ValueError("unversioned accounting cursor cannot have an update ID")

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "command_id": self.command_id,
            "cumulative_filled_quantity": decimal_string(self.cumulative_filled_quantity),
            "cumulative_quote_value": decimal_string(self.cumulative_quote_value),
            "cumulative_fee": decimal_string(self.cumulative_fee),
            "last_update_id": self.last_update_id,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OrderAccountingCursor:
        return cls(
            order_id=str(value["order_id"]),
            command_id=str(value["command_id"]),
            cumulative_filled_quantity=as_decimal(
                value.get("cumulative_filled_quantity", "0"),
                "cumulative_filled_quantity",
            ),
            cumulative_quote_value=as_decimal(
                value.get("cumulative_quote_value", "0"),
                "cumulative_quote_value",
            ),
            cumulative_fee=as_decimal(value.get("cumulative_fee", "0"), "cumulative_fee"),
            last_update_id=(
                None if value.get("last_update_id") is None else str(value["last_update_id"])
            ),
            updated_at=int(value.get("updated_at", 0)),
            version=int(value.get("version", 0)),
        )


@dataclass(frozen=True, slots=True)
class FillDelta:
    quantity: Decimal
    quote_value: Decimal
    fee: Decimal

    def __post_init__(self) -> None:
        quantity = as_decimal(self.quantity, "fill delta quantity")
        quote = as_decimal(self.quote_value, "fill delta quote_value")
        fee = as_decimal(self.fee, "fill delta fee")
        if min(quantity, quote, fee) < ZERO:
            raise ValueError("fill deltas must be non-negative")
        if quantity == ZERO and quote != ZERO:
            raise ValueError("zero-quantity fill delta cannot contain quote value")
        if quantity > ZERO and quote <= ZERO:
            raise ValueError("positive fill delta requires positive quote value")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "quote_value", quote)
        object.__setattr__(self, "fee", fee)

    @property
    def price(self) -> Decimal:
        if self.quantity <= ZERO:
            raise ValueError("zero fill delta has no execution price")
        return self.quote_value / self.quantity


@dataclass(frozen=True, slots=True)
class ReservationPlan:
    reservation: CommandReservation
    transaction: JournalTransaction | None


@dataclass(frozen=True, slots=True)
class FillAccountingResult:
    reservation: CommandReservation
    position: PortfolioPosition | None
    cursor: OrderAccountingCursor
    transaction: JournalTransaction
    realized_pnl: Decimal = ZERO
    position_closed: bool = False


@dataclass(frozen=True, slots=True)
class ReservationRelease:
    reservation: CommandReservation
    transaction: JournalTransaction | None


def _stable_identifier(prefix: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{prefix}:" + hashlib.sha256(canonical.encode()).hexdigest()


def reservation_id(account_id: str, command: ExecutionCommand) -> str:
    return _stable_identifier(
        "reservation",
        {
            "account_id": account_id,
            "command_id": command.command_id,
            "source_decision_id": command.source_decision_id,
            "action": command.action.value,
        },
    )


def position_id(account_id: str, symbol: str, command_id: str) -> str:
    return _stable_identifier(
        "position",
        {
            "account_id": account_id,
            "symbol": symbol,
            "open_command_id": command_id,
        },
    )


def _posting(
    account: JournalAccount,
    side: PostingSide,
    amount: Decimal,
    policy: AccountPolicy,
    memo: str,
) -> JournalPosting:
    return JournalPosting(
        account=account,
        side=side,
        amount=policy.quantize_money(amount),
        asset=policy.settlement_asset,
        memo=memo,
    )


def _validate_command_source(
    command: ExecutionCommand,
    decision: RiskDecision,
    policy: AccountPolicy,
) -> None:
    if decision.status is not RiskDecisionStatus.APPROVED:
        raise PortfolioAccountingError("account reservation requires an approved risk decision")
    if decision.storage_sequence is None:
        raise PortfolioAccountingError("account reservation requires a persisted risk decision")
    mismatches = []
    if command.source_decision_id != decision.decision_id:
        mismatches.append("decision_id")
    if command.source_decision_storage_sequence != decision.storage_sequence:
        mismatches.append("decision_storage_sequence")
    if command.account_id != decision.account_id:
        mismatches.append("account_id")
    if command.symbol != decision.symbol:
        mismatches.append("symbol")
    if command.action is not decision.approved_action:
        mismatches.append("approved_action")
    numeric = (
        ("quantity", command.quantity, decision.quantity),
        ("required_margin", command.required_margin, decision.required_margin),
        ("leverage", command.leverage, decision.leverage),
    )
    for name, left, right in numeric:
        if as_decimal(left, name) != as_decimal(right, name):
            mismatches.append(name)
    if command.reduce_only != decision.reduce_only:
        mismatches.append("reduce_only")
    if command.mode.value != policy.mode.value:
        mismatches.append("account_mode")
    if mismatches:
        raise PortfolioAccountingError(
            "execution command and risk decision differ: " + ", ".join(mismatches)
        )


def initialize_account_transaction(
    *,
    account_id: str,
    initial_capital: Decimal | int | float | str,
    event_time: int,
    source_id: str,
    policy: AccountPolicy,
) -> JournalTransaction:
    amount = policy.quantize_money(initial_capital)
    if amount <= ZERO:
        raise ValueError("initial capital must be positive")
    postings = (
        _posting(JournalAccount.CASH, PostingSide.DEBIT, amount, policy, "initial cash"),
        _posting(
            JournalAccount.EXTERNAL_CAPITAL,
            PostingSide.CREDIT,
            amount,
            policy,
            "initial contributed capital",
        ),
    )
    return make_journal_transaction(
        account_id=account_id,
        source_kind=JournalSourceKind.ACCOUNT_INITIALIZED,
        source_id=source_id,
        event_time=event_time,
        asset=policy.settlement_asset,
        postings=postings,
        evidence={"policy_id": policy.policy_id, "policy_version": policy.version},
    )


def deposit_transaction(
    *,
    account_id: str,
    amount: Decimal | int | float | str,
    event_time: int,
    source_id: str,
    policy: AccountPolicy,
) -> JournalTransaction:
    selected = policy.quantize_money(amount)
    if selected <= ZERO:
        raise ValueError("deposit amount must be positive")
    return make_journal_transaction(
        account_id=account_id,
        source_kind=JournalSourceKind.DEPOSIT,
        source_id=source_id,
        event_time=event_time,
        asset=policy.settlement_asset,
        postings=(
            _posting(JournalAccount.CASH, PostingSide.DEBIT, selected, policy, "deposit"),
            _posting(
                JournalAccount.EXTERNAL_CAPITAL,
                PostingSide.CREDIT,
                selected,
                policy,
                "contributed capital",
            ),
        ),
        evidence={},
    )


def withdrawal_transaction(
    *,
    account_id: str,
    amount: Decimal | int | float | str,
    event_time: int,
    source_id: str,
    policy: AccountPolicy,
) -> JournalTransaction:
    selected = policy.quantize_money(amount)
    if selected <= ZERO:
        raise ValueError("withdrawal amount must be positive")
    return make_journal_transaction(
        account_id=account_id,
        source_kind=JournalSourceKind.WITHDRAWAL,
        source_id=source_id,
        event_time=event_time,
        asset=policy.settlement_asset,
        postings=(
            _posting(
                JournalAccount.EXTERNAL_CAPITAL,
                PostingSide.DEBIT,
                selected,
                policy,
                "capital withdrawal",
            ),
            _posting(JournalAccount.CASH, PostingSide.CREDIT, selected, policy, "withdrawal"),
        ),
        evidence={},
    )


def plan_command_reservation(
    *,
    account_id: str,
    command: ExecutionCommand,
    decision: RiskDecision,
    balances: AccountBalances,
    policy: AccountPolicy,
    now_ms: int,
    current_position: PortfolioPosition | None = None,
    already_locked_exit_quantity: Decimal = ZERO,
) -> ReservationPlan:
    _validate_command_source(command, decision, policy)
    if command.created_at > now_ms:
        raise PortfolioAccountingError("command reservation time precedes command creation")
    if command.mode is ExecutionMode.SHADOW:
        state = ReservationState.SHADOW_RECORDED
        monetary_margin = ZERO
        fee_reserve = ZERO
    else:
        state = ReservationState.ACTIVE
        monetary_margin = policy.quantize_money(command.required_margin)
        fee_reserve = policy.quantize_money(
            as_decimal(command.quantity, "command quantity")
            * as_decimal(command.approval_price, "approval price")
            * policy.fee_reserve_rate
        )

    entry = command.action in {
        RiskApprovedAction.OPEN_LONG,
        RiskApprovedAction.OPEN_SHORT,
    }
    command_quantity = as_decimal(command.quantity, "command quantity")
    if entry:
        if current_position is not None:
            raise PortfolioAccountingError("entry reservation conflicts with an open position")
        reserved_exit = ZERO
    else:
        if current_position is None:
            raise PortfolioAccountingError("exit reservation requires an open position")
        expected_side = (
            PositionSide.LONG
            if command.action is RiskApprovedAction.CLOSE_LONG
            else PositionSide.SHORT
        )
        if current_position.side is not expected_side:
            raise PortfolioAccountingError("exit command targets another position side")
        available_close = current_position.quantity - as_decimal(
            already_locked_exit_quantity,
            "already_locked_exit_quantity",
        )
        if command_quantity > available_close:
            raise PortfolioAccountingError("exit reservations exceed the open position")
        monetary_margin = ZERO
        reserved_exit = command_quantity

    total_cash_reserve = monetary_margin + fee_reserve
    if total_cash_reserve > balances.cash:
        raise PortfolioAccountingError("insufficient cash for command reservation")

    reservation = CommandReservation(
        reservation_id=reservation_id(account_id, command),
        account_id=account_id,
        command_id=command.command_id,
        source_decision_id=command.source_decision_id,
        symbol=command.symbol,
        action=command.action,
        command_quantity=command_quantity,
        reserved_margin_total=monetary_margin,
        command_margin_remaining=monetary_margin,
        fee_reserve_total=fee_reserve,
        fee_reserve_remaining=fee_reserve,
        reserved_exit_quantity=reserved_exit,
        state=state,
        created_at=now_ms,
        updated_at=now_ms,
    )
    if total_cash_reserve == ZERO:
        return ReservationPlan(reservation, None)
    postings = []
    if monetary_margin > ZERO:
        postings.append(
            _posting(
                JournalAccount.COMMAND_MARGIN,
                PostingSide.DEBIT,
                monetary_margin,
                policy,
                "reserve command margin",
            )
        )
    if fee_reserve > ZERO:
        postings.append(
            _posting(
                JournalAccount.COMMAND_FEE_RESERVE,
                PostingSide.DEBIT,
                fee_reserve,
                policy,
                "reserve execution fees",
            )
        )
    postings.append(
        _posting(
            JournalAccount.CASH,
            PostingSide.CREDIT,
            total_cash_reserve,
            policy,
            "fund command reservation",
        )
    )
    transaction = make_journal_transaction(
        account_id=account_id,
        source_kind=JournalSourceKind.COMMAND_RESERVED,
        source_id=command.command_id,
        event_time=now_ms,
        asset=policy.settlement_asset,
        postings=tuple(postings),
        evidence={
            "reservation_id": reservation.reservation_id,
            "source_decision_id": command.source_decision_id,
            "symbol": command.symbol,
            "action": command.action.value,
        },
    )
    balances.apply(transaction)
    return ReservationPlan(reservation, transaction)


def derive_fill_delta(
    *,
    cursor: OrderAccountingCursor,
    order: ExecutionOrder,
    update: OrderUpdate,
    policy: AccountPolicy,
) -> tuple[FillDelta, OrderAccountingCursor]:
    if cursor.order_id != order.order_id or cursor.command_id != order.command_id:
        raise PortfolioAccountingError("order accounting cursor belongs to another order")
    if update.order_id != order.order_id:
        raise PortfolioAccountingError("order update belongs to another order")
    if update.event_time < cursor.updated_at:
        raise PortfolioAccountingError("accounting update time moved backwards")
    cumulative_quantity = as_decimal(
        update.cumulative_filled_quantity,
        "cumulative_filled_quantity",
    )
    cumulative_quote = policy.quantize_money(update.cumulative_quote_value)
    cumulative_fee = policy.quantize_money(update.cumulative_fee)
    quantity = cumulative_quantity - cursor.cumulative_filled_quantity
    quote = cumulative_quote - cursor.cumulative_quote_value
    fee = cumulative_fee - cursor.cumulative_fee
    if min(quantity, quote, fee) < ZERO:
        raise PortfolioAccountingError("execution cumulative values moved backwards")
    if quantity == ZERO and quote == ZERO and fee == ZERO:
        raise PortfolioAccountingError("order update contains no new accounting fact")
    if quantity == ZERO:
        raise PortfolioAccountingError("fee-only execution updates are not supported")
    delta = FillDelta(quantity, quote, fee)
    next_cursor = OrderAccountingCursor(
        order_id=cursor.order_id,
        command_id=cursor.command_id,
        cumulative_filled_quantity=cumulative_quantity,
        cumulative_quote_value=cumulative_quote,
        cumulative_fee=cumulative_fee,
        last_update_id=update.update_id,
        updated_at=update.event_time,
        version=cursor.version + 1,
    )
    return delta, next_cursor


def _fee_postings(
    *,
    fee: Decimal,
    reservation: CommandReservation,
    policy: AccountPolicy,
) -> tuple[list[JournalPosting], Decimal]:
    if fee == ZERO:
        return [], ZERO
    reserve_used = min(fee, reservation.fee_reserve_remaining)
    cash_used = fee - reserve_used
    postings = [_posting(JournalAccount.FEES, PostingSide.DEBIT, fee, policy, "execution fee")]
    if reserve_used > ZERO:
        postings.append(
            _posting(
                JournalAccount.COMMAND_FEE_RESERVE,
                PostingSide.CREDIT,
                reserve_used,
                policy,
                "consume fee reserve",
            )
        )
    if cash_used > ZERO:
        postings.append(
            _posting(
                JournalAccount.CASH,
                PostingSide.CREDIT,
                cash_used,
                policy,
                "execution fee above reserve",
            )
        )
    return postings, reserve_used


def apply_open_fill(
    *,
    command: ExecutionCommand,
    decision: RiskDecision,
    order: ExecutionOrder,
    update: OrderUpdate,
    cursor: OrderAccountingCursor,
    reservation: CommandReservation,
    balances: AccountBalances,
    policy: AccountPolicy,
    maintenance_margin_rate: Decimal | int | float | str,
    correlation_group: str = "default",
    position: PortfolioPosition | None = None,
) -> FillAccountingResult:
    _validate_command_source(command, decision, policy)
    if not reservation.entry or reservation.state is not ReservationState.ACTIVE:
        raise PortfolioAccountingError("open fill requires an active entry reservation")
    if order.leg_type is not OrderLegType.PRIMARY or order.reduce_only:
        raise PortfolioAccountingError("open fill requires a non-reduce-only primary order")
    delta, next_cursor = derive_fill_delta(
        cursor=cursor,
        order=order,
        update=update,
        policy=policy,
    )
    expected_side = (
        PositionSide.LONG if command.action is RiskApprovedAction.OPEN_LONG else PositionSide.SHORT
    )
    if position is not None:
        if position.side is not expected_side or position.open_command_id != command.command_id:
            raise PortfolioAccountingError("open fill conflicts with the existing position")
    filled_after = next_cursor.cumulative_filled_quantity
    command_quantity = reservation.command_quantity
    if filled_after > command_quantity:
        raise PortfolioAccountingError("accounted open fills exceed command quantity")
    if filled_after == command_quantity:
        margin_allocation = reservation.command_margin_remaining
    else:
        margin_allocation = policy.quantize_money(
            reservation.reserved_margin_total * delta.quantity / command_quantity
        )
        margin_allocation = min(margin_allocation, reservation.command_margin_remaining)
    if margin_allocation <= ZERO:
        raise PortfolioAccountingError("positive open fill has no allocatable position margin")

    postings = [
        _posting(
            JournalAccount.POSITION_MARGIN,
            PostingSide.DEBIT,
            margin_allocation,
            policy,
            "allocate position margin",
        ),
        _posting(
            JournalAccount.COMMAND_MARGIN,
            PostingSide.CREDIT,
            margin_allocation,
            policy,
            "consume command margin",
        ),
    ]
    fee_postings, fee_reserve_used = _fee_postings(
        fee=delta.fee,
        reservation=reservation,
        policy=policy,
    )
    postings.extend(fee_postings)
    transaction = make_journal_transaction(
        account_id=reservation.account_id,
        source_kind=JournalSourceKind.OPEN_FILL,
        source_id=update.update_id,
        event_time=update.event_time,
        asset=policy.settlement_asset,
        postings=tuple(postings),
        evidence={
            "command_id": command.command_id,
            "order_id": order.order_id,
            "fill_quantity": decimal_string(delta.quantity),
            "fill_quote_value": decimal_string(delta.quote_value),
            "fill_price": decimal_string(delta.price),
        },
    )
    balances.apply(transaction)

    initial_risk_delta = as_decimal(decision.initial_risk, "initial_risk") * (
        delta.quantity / command_quantity
    )
    margin_rate = as_decimal(maintenance_margin_rate, "maintenance_margin_rate")
    if decision.stop_price is None:
        raise PortfolioAccountingError("approved entry has no stop price")
    stop_price = as_decimal(decision.stop_price, "stop_price")
    if position is None:
        next_position = PortfolioPosition(
            position_id=position_id(reservation.account_id, command.symbol, command.command_id),
            account_id=reservation.account_id,
            symbol=command.symbol,
            side=expected_side,
            quantity=delta.quantity,
            average_entry_price=delta.price,
            mark_price=delta.price,
            stop_price=stop_price,
            position_margin=margin_allocation,
            initial_risk=initial_risk_delta,
            maintenance_margin_rate=margin_rate,
            correlation_group=correlation_group,
            open_command_id=command.command_id,
            open_risk_decision_id=decision.decision_id,
            open_intent_id=decision.intent_id,
            opened_at=update.event_time,
            updated_at=update.event_time,
        )
    else:
        total_quantity = position.quantity + delta.quantity
        average = (
            position.average_entry_price * position.quantity + delta.quote_value
        ) / total_quantity
        if position.stop_price != stop_price:
            raise PortfolioAccountingError("entry fills disagree on the approved stop")
        next_position = replace(
            position,
            quantity=total_quantity,
            average_entry_price=average,
            mark_price=delta.price,
            position_margin=position.position_margin + margin_allocation,
            initial_risk=position.initial_risk + initial_risk_delta,
            maintenance_margin_rate=margin_rate,
            updated_at=update.event_time,
            version=position.version + 1,
        )
    next_reservation = replace(
        reservation,
        command_margin_remaining=reservation.command_margin_remaining - margin_allocation,
        fee_reserve_remaining=reservation.fee_reserve_remaining - fee_reserve_used,
        updated_at=update.event_time,
        version=reservation.version + 1,
    )
    return FillAccountingResult(
        reservation=next_reservation,
        position=next_position,
        cursor=next_cursor,
        transaction=transaction,
    )


def apply_close_fill(
    *,
    command: ExecutionCommand,
    order: ExecutionOrder,
    update: OrderUpdate,
    cursor: OrderAccountingCursor,
    reservation: CommandReservation,
    balances: AccountBalances,
    policy: AccountPolicy,
    position: PortfolioPosition,
) -> FillAccountingResult:
    if reservation.state is not ReservationState.ACTIVE:
        raise PortfolioAccountingError("close fill requires an active reservation")
    if not order.reduce_only:
        raise PortfolioAccountingError("close fill requires reduce_only order semantics")
    delta, next_cursor = derive_fill_delta(
        cursor=cursor,
        order=order,
        update=update,
        policy=policy,
    )
    if delta.quantity > position.quantity:
        raise PortfolioAccountingError("reduce-only fill exceeds the open position")
    expected_action = (
        RiskApprovedAction.CLOSE_LONG
        if position.side is PositionSide.LONG
        else RiskApprovedAction.CLOSE_SHORT
    )
    if command.action not in {
        expected_action,
        RiskApprovedAction.OPEN_LONG,
        RiskApprovedAction.OPEN_SHORT,
    }:
        raise PortfolioAccountingError("close fill command targets another position")
    if reservation.entry:
        if command.command_id != position.open_command_id:
            raise PortfolioAccountingError("protective fill belongs to another open command")
        if order.leg_type is not OrderLegType.PROTECTIVE_STOP:
            raise PortfolioAccountingError("entry command may close only through protection")
    else:
        if reservation.action is not expected_action:
            raise PortfolioAccountingError("exit reservation targets another position")
        if delta.quantity > reservation.reserved_exit_quantity:
            raise PortfolioAccountingError("exit fill exceeds its quantity lock")

    full_close = delta.quantity == position.quantity
    if full_close:
        margin_release = position.position_margin
        risk_release = position.initial_risk
    else:
        margin_release = policy.quantize_money(
            position.position_margin * delta.quantity / position.quantity
        )
        risk_release = position.initial_risk * delta.quantity / position.quantity
    sign = Decimal("1") if position.side is PositionSide.LONG else Decimal("-1")
    realized = policy.quantize_money(
        sign * delta.quantity * (delta.price - position.average_entry_price)
    )

    postings = [
        _posting(
            JournalAccount.CASH,
            PostingSide.DEBIT,
            margin_release,
            policy,
            "release position margin",
        ),
        _posting(
            JournalAccount.POSITION_MARGIN,
            PostingSide.CREDIT,
            margin_release,
            policy,
            "consume position margin",
        ),
    ]
    if realized > ZERO:
        postings.extend(
            (
                _posting(
                    JournalAccount.CASH,
                    PostingSide.DEBIT,
                    realized,
                    policy,
                    "settle realized profit",
                ),
                _posting(
                    JournalAccount.REALIZED_PNL,
                    PostingSide.CREDIT,
                    realized,
                    policy,
                    "realized profit",
                ),
            )
        )
    elif realized < ZERO:
        loss = -realized
        postings.extend(
            (
                _posting(
                    JournalAccount.REALIZED_PNL,
                    PostingSide.DEBIT,
                    loss,
                    policy,
                    "realized loss",
                ),
                _posting(
                    JournalAccount.CASH,
                    PostingSide.CREDIT,
                    loss,
                    policy,
                    "settle realized loss",
                ),
            )
        )
    fee_postings, fee_reserve_used = _fee_postings(
        fee=delta.fee,
        reservation=reservation,
        policy=policy,
    )
    postings.extend(fee_postings)
    transaction = make_journal_transaction(
        account_id=reservation.account_id,
        source_kind=JournalSourceKind.CLOSE_FILL,
        source_id=update.update_id,
        event_time=update.event_time,
        asset=policy.settlement_asset,
        postings=tuple(postings),
        evidence={
            "command_id": command.command_id,
            "order_id": order.order_id,
            "position_id": position.position_id,
            "fill_quantity": decimal_string(delta.quantity),
            "fill_quote_value": decimal_string(delta.quote_value),
            "fill_price": decimal_string(delta.price),
            "realized_pnl": decimal_string(realized),
        },
    )
    balances.apply(transaction)

    next_position = None
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
    next_reservation = replace(
        reservation,
        fee_reserve_remaining=reservation.fee_reserve_remaining - fee_reserve_used,
        reserved_exit_quantity=(
            reservation.reserved_exit_quantity - delta.quantity
            if not reservation.entry
            else reservation.reserved_exit_quantity
        ),
        updated_at=update.event_time,
        version=reservation.version + 1,
    )
    return FillAccountingResult(
        reservation=next_reservation,
        position=next_position,
        cursor=next_cursor,
        transaction=transaction,
        realized_pnl=realized,
        position_closed=full_close,
    )


def release_command_reservation(
    *,
    reservation: CommandReservation,
    policy: AccountPolicy,
    event_time: int,
    reason: str,
) -> ReservationRelease:
    if reservation.state is ReservationState.SHADOW_RECORDED:
        return ReservationRelease(reservation, None)
    if reservation.state is ReservationState.RELEASED:
        return ReservationRelease(reservation, None)
    if event_time < reservation.updated_at:
        raise PortfolioAccountingError("reservation release time moved backwards")
    margin = reservation.command_margin_remaining
    fee = reservation.fee_reserve_remaining
    total = margin + fee
    next_reservation = replace(
        reservation,
        command_margin_remaining=ZERO,
        fee_reserve_remaining=ZERO,
        reserved_exit_quantity=ZERO,
        state=ReservationState.RELEASED,
        updated_at=event_time,
        version=reservation.version + 1,
    )
    if total == ZERO:
        return ReservationRelease(next_reservation, None)
    postings = [
        _posting(
            JournalAccount.CASH,
            PostingSide.DEBIT,
            total,
            policy,
            "release unused command reserve",
        )
    ]
    if margin > ZERO:
        postings.append(
            _posting(
                JournalAccount.COMMAND_MARGIN,
                PostingSide.CREDIT,
                margin,
                policy,
                "release command margin",
            )
        )
    if fee > ZERO:
        postings.append(
            _posting(
                JournalAccount.COMMAND_FEE_RESERVE,
                PostingSide.CREDIT,
                fee,
                policy,
                "release fee reserve",
            )
        )
    transaction = make_journal_transaction(
        account_id=reservation.account_id,
        source_kind=JournalSourceKind.COMMAND_RELEASED,
        source_id=f"{reservation.command_id}:{reservation.version + 1}",
        event_time=event_time,
        asset=policy.settlement_asset,
        postings=tuple(postings),
        evidence={
            "command_id": reservation.command_id,
            "reservation_id": reservation.reservation_id,
            "reason": reason,
        },
    )
    return ReservationRelease(next_reservation, transaction)


def funding_transaction(
    *,
    account_id: str,
    position: PortfolioPosition,
    rate: Decimal | int | float | str,
    mark_price: Decimal | int | float | str,
    event_time: int,
    source_id: str,
    policy: AccountPolicy,
) -> JournalTransaction:
    selected_rate = as_decimal(rate, "funding rate")
    selected_mark = as_decimal(mark_price, "funding mark price")
    if selected_mark <= ZERO:
        raise ValueError("funding mark price must be positive")
    sign = Decimal("1") if position.side is PositionSide.LONG else Decimal("-1")
    payment = policy.quantize_money(sign * position.quantity * selected_mark * selected_rate)
    if payment == ZERO:
        raise PortfolioAccountingError("funding amount rounds to zero")
    if payment > ZERO:
        postings = (
            _posting(
                JournalAccount.FUNDING_PNL,
                PostingSide.DEBIT,
                payment,
                policy,
                "funding paid",
            ),
            _posting(
                JournalAccount.CASH,
                PostingSide.CREDIT,
                payment,
                policy,
                "settle funding payment",
            ),
        )
    else:
        receipt = -payment
        postings = (
            _posting(
                JournalAccount.CASH,
                PostingSide.DEBIT,
                receipt,
                policy,
                "settle funding receipt",
            ),
            _posting(
                JournalAccount.FUNDING_PNL,
                PostingSide.CREDIT,
                receipt,
                policy,
                "funding received",
            ),
        )
    return make_journal_transaction(
        account_id=account_id,
        source_kind=JournalSourceKind.FUNDING,
        source_id=source_id,
        event_time=event_time,
        asset=policy.settlement_asset,
        postings=postings,
        evidence={
            "position_id": position.position_id,
            "symbol": position.symbol,
            "rate": decimal_string(selected_rate),
            "mark_price": decimal_string(selected_mark),
            "signed_payment": decimal_string(payment),
        },
    )


def apply_mark_price(
    position: PortfolioPosition,
    *,
    mark_price: Decimal | int | float | str,
    observed_at: int,
) -> PortfolioPosition:
    selected = as_decimal(mark_price, "mark_price")
    if selected <= ZERO:
        raise ValueError("mark_price must be positive")
    if observed_at < position.updated_at:
        raise PortfolioAccountingError("mark price time moved backwards")
    if observed_at == position.updated_at:
        if selected != position.mark_price:
            raise PortfolioAccountingError("same-time mark price revision")
        return position
    return replace(
        position,
        mark_price=selected,
        updated_at=observed_at,
        version=position.version + 1,
    )


def build_account_snapshot(
    *,
    account_id: str,
    observed_at: int,
    balances: AccountBalances,
    positions: tuple[PortfolioPosition, ...],
    day_start_equity: Decimal | int | float | str,
    peak_equity: Decimal | int | float | str,
    consecutive_losses: int,
    watermarks: AccountWatermarks,
    global_halt: bool = False,
    day_halt: bool = False,
    drawdown_halt: bool = False,
    halt_reasons=(),
) -> AccountSnapshot:
    sorted_positions = tuple(sorted(positions, key=lambda item: item.symbol))
    day_start = as_decimal(day_start_equity, "day_start_equity")
    peak = as_decimal(peak_equity, "peak_equity")
    reasons = tuple(halt_reasons)
    snapshot_id = account_snapshot_id(
        account_id=account_id,
        observed_at=observed_at,
        balances=balances,
        positions=sorted_positions,
        day_start_equity=day_start,
        peak_equity=peak,
        consecutive_losses=consecutive_losses,
        watermarks=watermarks,
        global_halt=global_halt,
        day_halt=day_halt,
        drawdown_halt=drawdown_halt,
        halt_reasons=reasons,
    )
    return AccountSnapshot(
        snapshot_id=snapshot_id,
        account_id=account_id,
        observed_at=observed_at,
        balances=balances,
        positions=sorted_positions,
        day_start_equity=day_start,
        peak_equity=peak,
        consecutive_losses=consecutive_losses,
        watermarks=watermarks,
        global_halt=global_halt,
        day_halt=day_halt,
        drawdown_halt=drawdown_halt,
        halt_reasons=reasons,
    )
