from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from .risk_contracts import (
    AccountRiskSnapshot,
    PositionRiskSnapshot,
    RiskApprovedAction,
)

ZERO = Decimal("0")


class AccountMode(StrEnum):
    SHADOW = "shadow"
    SIMULATION = "simulation"


class PostingSide(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class JournalAccount(StrEnum):
    CASH = "cash"
    COMMAND_MARGIN = "command_margin"
    POSITION_MARGIN = "position_margin"
    COMMAND_FEE_RESERVE = "command_fee_reserve"
    EXTERNAL_CAPITAL = "external_capital"
    REALIZED_PNL = "realized_pnl"
    FEES = "fees"
    FUNDING_PNL = "funding_pnl"


class JournalSourceKind(StrEnum):
    ACCOUNT_INITIALIZED = "account_initialized"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    COMMAND_RESERVED = "command_reserved"
    COMMAND_RELEASED = "command_released"
    OPEN_FILL = "open_fill"
    CLOSE_FILL = "close_fill"
    FUNDING = "funding"


class ReservationState(StrEnum):
    SHADOW_RECORDED = "shadow_recorded"
    ACTIVE = "active"
    RELEASED = "released"
    FAILED = "failed"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class AccountHaltReason(StrEnum):
    ACCOUNTING_INVARIANT = "accounting_invariant"
    NEGATIVE_ASSET = "negative_asset"
    RECONCILIATION_FAILURE = "reconciliation_failure"
    STALE_MARK = "stale_mark"
    EXECUTION_CONFLICT = "execution_conflict"


_ASSET_ACCOUNTS = frozenset(
    {
        JournalAccount.CASH,
        JournalAccount.COMMAND_MARGIN,
        JournalAccount.POSITION_MARGIN,
        JournalAccount.COMMAND_FEE_RESERVE,
    }
)
_EXPENSE_ACCOUNTS = frozenset({JournalAccount.FEES})
_INCOME_EQUITY_ACCOUNTS = frozenset(
    {
        JournalAccount.EXTERNAL_CAPITAL,
        JournalAccount.REALIZED_PNL,
        JournalAccount.FUNDING_PNL,
    }
)


def as_decimal(value: Decimal | int | float | str, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} is not a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def decimal_string(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("cannot serialize a non-finite decimal")
    return format(value, "f")


def _strict_json(value: Any, path: str = "evidence") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError(f"{path} contains a non-finite float")
        return
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{path} contains a non-finite decimal")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            _strict_json(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _strict_json(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains unsupported type: {type(value).__name__}")


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_string(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AccountPolicy:
    policy_id: str = "simulation-cross-margin-account"
    version: str = "1.0.0"
    mode: AccountMode = AccountMode.SIMULATION
    settlement_asset: str = "USDT"
    settlement_quantum: Decimal = Decimal("0.00000001")
    fee_reserve_rate: Decimal = Decimal("0.002")
    max_mark_age_ms: int = 30_000

    def __post_init__(self) -> None:
        if not self.policy_id or not self.version or not self.settlement_asset:
            raise ValueError("account policy identifiers must not be empty")
        quantum = as_decimal(self.settlement_quantum, "settlement_quantum")
        fee_rate = as_decimal(self.fee_reserve_rate, "fee_reserve_rate")
        if quantum <= ZERO:
            raise ValueError("settlement_quantum must be positive")
        if quantum.as_tuple().exponent >= 0:
            raise ValueError("settlement_quantum must define a fractional precision")
        if not ZERO <= fee_rate <= Decimal("0.1"):
            raise ValueError("fee_reserve_rate must be in [0, 0.1]")
        if self.max_mark_age_ms < 0:
            raise ValueError("max_mark_age_ms must be non-negative")
        object.__setattr__(self, "settlement_quantum", quantum)
        object.__setattr__(self, "fee_reserve_rate", fee_rate)

    def quantize_money(self, value: Decimal | int | float | str) -> Decimal:
        return as_decimal(value, "money").quantize(self.settlement_quantum)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "mode": self.mode.value,
            "settlement_asset": self.settlement_asset,
            "settlement_quantum": decimal_string(self.settlement_quantum),
            "fee_reserve_rate": decimal_string(self.fee_reserve_rate),
            "max_mark_age_ms": self.max_mark_age_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AccountPolicy:
        return cls(
            policy_id=str(value["policy_id"]),
            version=str(value["version"]),
            mode=AccountMode(value["mode"]),
            settlement_asset=str(value["settlement_asset"]),
            settlement_quantum=as_decimal(value["settlement_quantum"], "settlement_quantum"),
            fee_reserve_rate=as_decimal(value["fee_reserve_rate"], "fee_reserve_rate"),
            max_mark_age_ms=int(value["max_mark_age_ms"]),
        )


def account_policy_hash(policy: AccountPolicy) -> str:
    canonical = json.dumps(
        policy.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class JournalPosting:
    account: JournalAccount
    side: PostingSide
    amount: Decimal
    asset: str = "USDT"
    memo: str | None = None

    def __post_init__(self) -> None:
        amount = as_decimal(self.amount, "posting amount")
        if amount <= ZERO:
            raise ValueError("posting amount must be positive")
        if not self.asset:
            raise ValueError("posting asset must not be empty")
        if self.memo is not None and not self.memo:
            raise ValueError("posting memo must be non-empty when provided")
        object.__setattr__(self, "amount", amount)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account.value,
            "side": self.side.value,
            "amount": decimal_string(self.amount),
            "asset": self.asset,
            "memo": self.memo,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> JournalPosting:
        return cls(
            account=JournalAccount(value["account"]),
            side=PostingSide(value["side"]),
            amount=as_decimal(value["amount"], "posting amount"),
            asset=str(value["asset"]),
            memo=None if value.get("memo") is None else str(value["memo"]),
        )


@dataclass(frozen=True, slots=True)
class JournalTransaction:
    transaction_id: str
    account_id: str
    source_kind: JournalSourceKind
    source_id: str
    event_time: int
    asset: str
    postings: tuple[JournalPosting, ...]
    evidence: dict[str, Any]
    storage_sequence: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.transaction_id or not self.account_id or not self.source_id or not self.asset:
            raise ValueError("journal transaction identifiers must not be empty")
        if self.event_time < 0:
            raise ValueError("journal event_time must be non-negative")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("journal storage_sequence must be positive")
        if self.schema_version <= 0:
            raise ValueError("journal schema_version must be positive")
        if len(self.postings) < 2:
            raise ValueError("journal transaction requires at least two postings")
        if any(posting.asset != self.asset for posting in self.postings):
            raise ValueError("journal transaction contains another settlement asset")
        debit = sum(
            (posting.amount for posting in self.postings if posting.side is PostingSide.DEBIT),
            ZERO,
        )
        credit = sum(
            (posting.amount for posting in self.postings if posting.side is PostingSide.CREDIT),
            ZERO,
        )
        if debit != credit:
            raise ValueError(f"journal transaction is not balanced: {debit} != {credit}")
        _strict_json(self.evidence)
        json.dumps(_json_value(self.evidence), sort_keys=True, allow_nan=False)

    @property
    def debit_total(self) -> Decimal:
        return sum(
            (posting.amount for posting in self.postings if posting.side is PostingSide.DEBIT),
            ZERO,
        )

    @property
    def credit_total(self) -> Decimal:
        return sum(
            (posting.amount for posting in self.postings if posting.side is PostingSide.CREDIT),
            ZERO,
        )

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "account_id": self.account_id,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "event_time": self.event_time,
            "asset": self.asset,
            "postings": [posting.to_dict() for posting in self.postings],
            "evidence": _json_value(self.evidence),
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_dict(),
            "storage_sequence": self.storage_sequence,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> JournalTransaction:
        return cls(
            transaction_id=str(value["transaction_id"]),
            account_id=str(value["account_id"]),
            source_kind=JournalSourceKind(value["source_kind"]),
            source_id=str(value["source_id"]),
            event_time=int(value["event_time"]),
            asset=str(value["asset"]),
            postings=tuple(
                JournalPosting.from_dict(posting) for posting in value.get("postings", [])
            ),
            evidence=dict(value.get("evidence") or {}),
            storage_sequence=(
                None if value.get("storage_sequence") is None else int(value["storage_sequence"])
            ),
            schema_version=int(value.get("schema_version", 1)),
        )

    def with_storage_sequence(self, sequence: int) -> JournalTransaction:
        if sequence <= 0:
            raise ValueError("journal storage sequence must be positive")
        return replace(self, storage_sequence=sequence)


def journal_transaction_id(
    *,
    account_id: str,
    source_kind: JournalSourceKind,
    source_id: str,
    event_time: int,
    asset: str,
    postings: tuple[JournalPosting, ...],
    evidence: dict[str, Any],
    schema_version: int = 1,
) -> str:
    canonical = json.dumps(
        {
            "account_id": account_id,
            "source_kind": source_kind.value,
            "source_id": source_id,
            "event_time": event_time,
            "asset": asset,
            "postings": [posting.to_dict() for posting in postings],
            "evidence": _json_value(evidence),
            "schema_version": schema_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "journal:" + hashlib.sha256(canonical.encode()).hexdigest()


def make_journal_transaction(
    *,
    account_id: str,
    source_kind: JournalSourceKind,
    source_id: str,
    event_time: int,
    asset: str,
    postings: tuple[JournalPosting, ...],
    evidence: dict[str, Any] | None = None,
) -> JournalTransaction:
    selected_evidence = evidence or {}
    return JournalTransaction(
        transaction_id=journal_transaction_id(
            account_id=account_id,
            source_kind=source_kind,
            source_id=source_id,
            event_time=event_time,
            asset=asset,
            postings=postings,
            evidence=selected_evidence,
        ),
        account_id=account_id,
        source_kind=source_kind,
        source_id=source_id,
        event_time=event_time,
        asset=asset,
        postings=postings,
        evidence=selected_evidence,
    )


@dataclass(frozen=True, slots=True)
class AccountBalances:
    cash: Decimal = ZERO
    command_margin: Decimal = ZERO
    position_margin: Decimal = ZERO
    command_fee_reserve: Decimal = ZERO
    external_capital: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    funding_pnl: Decimal = ZERO

    def __post_init__(self) -> None:
        for name in (
            "cash",
            "command_margin",
            "position_margin",
            "command_fee_reserve",
            "external_capital",
            "realized_pnl",
            "fees",
            "funding_pnl",
        ):
            value = as_decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        self.assert_valid()

    @property
    def settled_assets(self) -> Decimal:
        return self.cash + self.command_margin + self.position_margin + self.command_fee_reserve

    @property
    def accounting_equity(self) -> Decimal:
        return self.external_capital + self.realized_pnl + self.funding_pnl - self.fees

    def balance(self, account: JournalAccount) -> Decimal:
        return getattr(self, account.value)

    def assert_valid(self) -> None:
        for account in _ASSET_ACCOUNTS:
            if self.balance(account) < ZERO:
                raise ValueError(f"asset balance cannot be negative: {account.value}")
        if self.fees < ZERO:
            raise ValueError("fees expense balance cannot be negative")
        if self.settled_assets != self.accounting_equity:
            raise ValueError(
                "accounting identity failed: "
                f"assets={self.settled_assets} equity={self.accounting_equity}"
            )

    def apply(self, transaction: JournalTransaction) -> AccountBalances:
        values = {account.value: self.balance(account) for account in JournalAccount}
        for posting in transaction.postings:
            normal_debit = posting.account in _ASSET_ACCOUNTS | _EXPENSE_ACCOUNTS
            increase = posting.side is (PostingSide.DEBIT if normal_debit else PostingSide.CREDIT)
            values[posting.account.value] += posting.amount if increase else -posting.amount
        result = AccountBalances(**values)
        result.assert_valid()
        return result

    def to_dict(self) -> dict[str, str]:
        return {account.value: decimal_string(self.balance(account)) for account in JournalAccount}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AccountBalances:
        return cls(
            **{
                account.value: as_decimal(value.get(account.value, "0"), account.value)
                for account in JournalAccount
            }
        )


@dataclass(frozen=True, slots=True)
class CommandReservation:
    reservation_id: str
    account_id: str
    command_id: str
    source_decision_id: str
    symbol: str
    action: RiskApprovedAction
    command_quantity: Decimal
    reserved_margin_total: Decimal
    command_margin_remaining: Decimal
    fee_reserve_total: Decimal
    fee_reserve_remaining: Decimal
    reserved_exit_quantity: Decimal
    state: ReservationState
    created_at: int
    updated_at: int
    version: int = 1
    storage_sequence: int | None = None

    def __post_init__(self) -> None:
        identifiers = (
            self.reservation_id,
            self.account_id,
            self.command_id,
            self.source_decision_id,
            self.symbol,
        )
        if any(not value for value in identifiers):
            raise ValueError("command reservation identifiers must not be empty")
        if min(self.created_at, self.updated_at, self.version) < 0:
            raise ValueError("command reservation counters must be non-negative")
        if self.updated_at < self.created_at or self.version <= 0:
            raise ValueError("invalid command reservation chronology or version")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("reservation storage_sequence must be positive")
        for name in (
            "command_quantity",
            "reserved_margin_total",
            "command_margin_remaining",
            "fee_reserve_total",
            "fee_reserve_remaining",
            "reserved_exit_quantity",
        ):
            value = as_decimal(getattr(self, name), name)
            if value < ZERO:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        if self.command_quantity <= ZERO:
            raise ValueError("command_quantity must be positive")
        if self.command_margin_remaining > self.reserved_margin_total:
            raise ValueError("remaining command margin exceeds the original reservation")
        if self.fee_reserve_remaining > self.fee_reserve_total:
            raise ValueError("remaining fee reserve exceeds the original reservation")
        entry = self.action in {
            RiskApprovedAction.OPEN_LONG,
            RiskApprovedAction.OPEN_SHORT,
        }
        if entry:
            if self.reserved_exit_quantity != ZERO:
                raise ValueError("entry reservation cannot lock exit quantity")
        else:
            if self.reserved_margin_total != ZERO:
                raise ValueError("exit reservation cannot reserve new margin")
            if self.reserved_exit_quantity <= ZERO:
                raise ValueError("exit reservation requires a positive quantity lock")
        if self.state in {ReservationState.RELEASED, ReservationState.SHADOW_RECORDED}:
            if self.command_margin_remaining != ZERO or self.fee_reserve_remaining != ZERO:
                raise ValueError("released reservation cannot retain monetary reserves")

    @property
    def entry(self) -> bool:
        return self.action in {
            RiskApprovedAction.OPEN_LONG,
            RiskApprovedAction.OPEN_SHORT,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "account_id": self.account_id,
            "command_id": self.command_id,
            "source_decision_id": self.source_decision_id,
            "symbol": self.symbol,
            "action": self.action.value,
            "command_quantity": decimal_string(self.command_quantity),
            "reserved_margin_total": decimal_string(self.reserved_margin_total),
            "command_margin_remaining": decimal_string(self.command_margin_remaining),
            "fee_reserve_total": decimal_string(self.fee_reserve_total),
            "fee_reserve_remaining": decimal_string(self.fee_reserve_remaining),
            "reserved_exit_quantity": decimal_string(self.reserved_exit_quantity),
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "storage_sequence": self.storage_sequence,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CommandReservation:
        return cls(
            reservation_id=str(value["reservation_id"]),
            account_id=str(value["account_id"]),
            command_id=str(value["command_id"]),
            source_decision_id=str(value["source_decision_id"]),
            symbol=str(value["symbol"]),
            action=RiskApprovedAction(value["action"]),
            command_quantity=as_decimal(value["command_quantity"], "command_quantity"),
            reserved_margin_total=as_decimal(
                value["reserved_margin_total"], "reserved_margin_total"
            ),
            command_margin_remaining=as_decimal(
                value["command_margin_remaining"], "command_margin_remaining"
            ),
            fee_reserve_total=as_decimal(value["fee_reserve_total"], "fee_reserve_total"),
            fee_reserve_remaining=as_decimal(
                value["fee_reserve_remaining"], "fee_reserve_remaining"
            ),
            reserved_exit_quantity=as_decimal(
                value["reserved_exit_quantity"], "reserved_exit_quantity"
            ),
            state=ReservationState(value["state"]),
            created_at=int(value["created_at"]),
            updated_at=int(value["updated_at"]),
            version=int(value.get("version", 1)),
            storage_sequence=(
                None if value.get("storage_sequence") is None else int(value["storage_sequence"])
            ),
        )

    def with_storage_sequence(self, sequence: int) -> CommandReservation:
        if sequence <= 0:
            raise ValueError("reservation storage sequence must be positive")
        return replace(self, storage_sequence=sequence)


@dataclass(frozen=True, slots=True)
class PortfolioPosition:
    position_id: str
    account_id: str
    symbol: str
    side: PositionSide
    quantity: Decimal
    average_entry_price: Decimal
    mark_price: Decimal
    stop_price: Decimal
    position_margin: Decimal
    initial_risk: Decimal
    maintenance_margin_rate: Decimal
    correlation_group: str
    open_command_id: str
    open_risk_decision_id: str
    open_intent_id: str
    opened_at: int
    updated_at: int
    version: int = 1

    def __post_init__(self) -> None:
        identifiers = (
            self.position_id,
            self.account_id,
            self.symbol,
            self.correlation_group,
            self.open_command_id,
            self.open_risk_decision_id,
            self.open_intent_id,
        )
        if any(not value for value in identifiers):
            raise ValueError("position identifiers must not be empty")
        if min(self.opened_at, self.updated_at, self.version) < 0:
            raise ValueError("position counters must be non-negative")
        if self.updated_at < self.opened_at or self.version <= 0:
            raise ValueError("invalid position chronology or version")
        for name in (
            "quantity",
            "average_entry_price",
            "mark_price",
            "stop_price",
            "position_margin",
            "initial_risk",
            "maintenance_margin_rate",
        ):
            value = as_decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        if (
            min(
                self.quantity,
                self.average_entry_price,
                self.mark_price,
                self.stop_price,
            )
            <= ZERO
        ):
            raise ValueError("position quantity and prices must be positive")
        if self.side is PositionSide.LONG and self.stop_price >= self.average_entry_price:
            raise ValueError("long position stop must be below average entry")
        if self.side is PositionSide.SHORT and self.stop_price <= self.average_entry_price:
            raise ValueError("short position stop must be above average entry")
        if min(self.position_margin, self.initial_risk) < ZERO:
            raise ValueError("position margin and initial risk must be non-negative")
        if not ZERO < self.maintenance_margin_rate < Decimal("1"):
            raise ValueError("invalid maintenance_margin_rate")

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side is PositionSide.LONG else -self.quantity

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.mark_price

    @property
    def unrealized_pnl(self) -> Decimal:
        sign = Decimal("1") if self.side is PositionSide.LONG else Decimal("-1")
        return sign * self.quantity * (self.mark_price - self.average_entry_price)

    @property
    def maintenance_margin(self) -> Decimal:
        return self.notional * self.maintenance_margin_rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": decimal_string(self.quantity),
            "average_entry_price": decimal_string(self.average_entry_price),
            "mark_price": decimal_string(self.mark_price),
            "stop_price": decimal_string(self.stop_price),
            "position_margin": decimal_string(self.position_margin),
            "initial_risk": decimal_string(self.initial_risk),
            "maintenance_margin_rate": decimal_string(self.maintenance_margin_rate),
            "correlation_group": self.correlation_group,
            "open_command_id": self.open_command_id,
            "open_risk_decision_id": self.open_risk_decision_id,
            "open_intent_id": self.open_intent_id,
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PortfolioPosition:
        return cls(
            position_id=str(value["position_id"]),
            account_id=str(value["account_id"]),
            symbol=str(value["symbol"]),
            side=PositionSide(value["side"]),
            quantity=as_decimal(value["quantity"], "quantity"),
            average_entry_price=as_decimal(value["average_entry_price"], "average_entry_price"),
            mark_price=as_decimal(value["mark_price"], "mark_price"),
            stop_price=as_decimal(value["stop_price"], "stop_price"),
            position_margin=as_decimal(value["position_margin"], "position_margin"),
            initial_risk=as_decimal(value["initial_risk"], "initial_risk"),
            maintenance_margin_rate=as_decimal(
                value["maintenance_margin_rate"], "maintenance_margin_rate"
            ),
            correlation_group=str(value["correlation_group"]),
            open_command_id=str(value["open_command_id"]),
            open_risk_decision_id=str(value["open_risk_decision_id"]),
            open_intent_id=str(value["open_intent_id"]),
            opened_at=int(value["opened_at"]),
            updated_at=int(value["updated_at"]),
            version=int(value.get("version", 1)),
        )


@dataclass(frozen=True, slots=True)
class AccountWatermarks:
    execution_command_sequence: int = 0
    execution_update_sequence: int = 0
    market_sequence: int = 0
    funding_sequence: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.execution_command_sequence,
                self.execution_update_sequence,
                self.market_sequence,
                self.funding_sequence,
            )
            < 0
        ):
            raise ValueError("account watermarks must be non-negative")

    def to_dict(self) -> dict[str, int]:
        return {
            "execution_command_sequence": self.execution_command_sequence,
            "execution_update_sequence": self.execution_update_sequence,
            "market_sequence": self.market_sequence,
            "funding_sequence": self.funding_sequence,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AccountWatermarks:
        return cls(
            execution_command_sequence=int(value.get("execution_command_sequence", 0)),
            execution_update_sequence=int(value.get("execution_update_sequence", 0)),
            market_sequence=int(value.get("market_sequence", 0)),
            funding_sequence=int(value.get("funding_sequence", 0)),
        )


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    snapshot_id: str
    account_id: str
    observed_at: int
    balances: AccountBalances
    positions: tuple[PortfolioPosition, ...]
    day_start_equity: Decimal
    peak_equity: Decimal
    consecutive_losses: int
    watermarks: AccountWatermarks
    global_halt: bool = False
    day_halt: bool = False
    drawdown_halt: bool = False
    halt_reasons: tuple[AccountHaltReason, ...] = ()
    storage_sequence: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.account_id:
            raise ValueError("account snapshot identifiers must not be empty")
        if min(self.observed_at, self.consecutive_losses) < 0:
            raise ValueError("account snapshot counters must be non-negative")
        if self.storage_sequence is not None and self.storage_sequence <= 0:
            raise ValueError("snapshot storage_sequence must be positive")
        if self.schema_version <= 0:
            raise ValueError("snapshot schema_version must be positive")
        day_start = as_decimal(self.day_start_equity, "day_start_equity")
        peak = as_decimal(self.peak_equity, "peak_equity")
        if min(day_start, peak) < ZERO:
            raise ValueError("snapshot equity anchors must be non-negative")
        object.__setattr__(self, "day_start_equity", day_start)
        object.__setattr__(self, "peak_equity", peak)
        symbols = [position.symbol for position in self.positions]
        if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
            raise ValueError("snapshot positions must be unique and sorted by symbol")
        if any(position.account_id != self.account_id for position in self.positions):
            raise ValueError("snapshot contains a position from another account")
        if any(position.updated_at > self.observed_at for position in self.positions):
            raise ValueError("snapshot precedes one of its positions")
        if len(self.halt_reasons) != len(set(self.halt_reasons)):
            raise ValueError("snapshot halt reasons must be unique")

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((position.unrealized_pnl for position in self.positions), ZERO)

    @property
    def equity(self) -> Decimal:
        return self.balances.settled_assets + self.unrealized_pnl

    @property
    def available_balance(self) -> Decimal:
        return self.balances.cash

    @property
    def maintenance_margin(self) -> Decimal:
        return sum((position.maintenance_margin for position in self.positions), ZERO)

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "account_id": self.account_id,
            "observed_at": self.observed_at,
            "balances": self.balances.to_dict(),
            "positions": [position.to_dict() for position in self.positions],
            "day_start_equity": decimal_string(self.day_start_equity),
            "peak_equity": decimal_string(self.peak_equity),
            "consecutive_losses": self.consecutive_losses,
            "watermarks": self.watermarks.to_dict(),
            "global_halt": self.global_halt,
            "day_halt": self.day_halt,
            "drawdown_halt": self.drawdown_halt,
            "halt_reasons": [reason.value for reason in self.halt_reasons],
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_dict(),
            "unrealized_pnl": decimal_string(self.unrealized_pnl),
            "equity": decimal_string(self.equity),
            "available_balance": decimal_string(self.available_balance),
            "maintenance_margin": decimal_string(self.maintenance_margin),
            "storage_sequence": self.storage_sequence,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AccountSnapshot:
        snapshot = cls(
            snapshot_id=str(value["snapshot_id"]),
            account_id=str(value["account_id"]),
            observed_at=int(value["observed_at"]),
            balances=AccountBalances.from_dict(dict(value["balances"])),
            positions=tuple(
                PortfolioPosition.from_dict(position) for position in value.get("positions", [])
            ),
            day_start_equity=as_decimal(
                value["day_start_equity"],
                "day_start_equity",
            ),
            peak_equity=as_decimal(value["peak_equity"], "peak_equity"),
            consecutive_losses=int(value["consecutive_losses"]),
            watermarks=AccountWatermarks.from_dict(dict(value["watermarks"])),
            global_halt=bool(value.get("global_halt", False)),
            day_halt=bool(value.get("day_halt", False)),
            drawdown_halt=bool(value.get("drawdown_halt", False)),
            halt_reasons=tuple(
                AccountHaltReason(reason) for reason in value.get("halt_reasons", [])
            ),
            storage_sequence=(
                None if value.get("storage_sequence") is None else int(value["storage_sequence"])
            ),
            schema_version=int(value.get("schema_version", 1)),
        )
        derived = {
            "unrealized_pnl": snapshot.unrealized_pnl,
            "equity": snapshot.equity,
            "available_balance": snapshot.available_balance,
            "maintenance_margin": snapshot.maintenance_margin,
        }
        for name, expected in derived.items():
            if name in value and as_decimal(value[name], name) != expected:
                raise ValueError(f"serialized account snapshot has inconsistent {name}")
        return snapshot

    def with_storage_sequence(self, sequence: int) -> AccountSnapshot:
        if sequence <= 0:
            raise ValueError("snapshot storage sequence must be positive")
        return replace(self, storage_sequence=sequence)

    def to_risk_snapshot(self) -> AccountRiskSnapshot:
        return AccountRiskSnapshot(
            snapshot_id=self.snapshot_id,
            account_id=self.account_id,
            observed_at=self.observed_at,
            equity=float(self.equity),
            available_balance=float(self.available_balance),
            maintenance_margin=float(self.maintenance_margin),
            day_start_equity=float(self.day_start_equity),
            peak_equity=float(self.peak_equity),
            consecutive_losses=self.consecutive_losses,
            positions=tuple(
                PositionRiskSnapshot(
                    symbol=position.symbol,
                    side=position.side.value,
                    quantity=float(position.quantity),
                    entry_price=float(position.average_entry_price),
                    mark_price=float(position.mark_price),
                    initial_risk=float(position.initial_risk),
                    maintenance_margin_rate=float(position.maintenance_margin_rate),
                    correlation_group=position.correlation_group,
                    stop_price=float(position.stop_price),
                )
                for position in self.positions
            ),
            global_halt=self.global_halt,
            day_halt=self.day_halt,
            drawdown_halt=self.drawdown_halt,
            sequence=self.storage_sequence or 0,
        )


def account_snapshot_id(
    *,
    account_id: str,
    observed_at: int,
    balances: AccountBalances,
    positions: tuple[PortfolioPosition, ...],
    day_start_equity: Decimal,
    peak_equity: Decimal,
    consecutive_losses: int,
    watermarks: AccountWatermarks,
    global_halt: bool,
    day_halt: bool,
    drawdown_halt: bool,
    halt_reasons: tuple[AccountHaltReason, ...],
) -> str:
    canonical = json.dumps(
        {
            "account_id": account_id,
            "observed_at": observed_at,
            "balances": balances.to_dict(),
            "positions": [position.to_dict() for position in positions],
            "day_start_equity": decimal_string(day_start_equity),
            "peak_equity": decimal_string(peak_equity),
            "consecutive_losses": consecutive_losses,
            "watermarks": watermarks.to_dict(),
            "global_halt": global_halt,
            "day_halt": day_halt,
            "drawdown_halt": drawdown_halt,
            "halt_reasons": [reason.value for reason in halt_reasons],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "account_snapshot:" + hashlib.sha256(canonical.encode()).hexdigest()
