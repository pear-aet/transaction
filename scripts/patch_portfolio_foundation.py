from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


CONTRACTS = "src/crypto_quant/portfolio_contracts.py"
ACCOUNTING = "src/crypto_quant/portfolio_accounting.py"
STORE = "src/crypto_quant/portfolio_store.py"

replace_once(
    CONTRACTS,
    '''    average_entry_price: Decimal
    mark_price: Decimal
    position_margin: Decimal
''',
    '''    average_entry_price: Decimal
    mark_price: Decimal
    stop_price: Decimal
    position_margin: Decimal
''',
)
replace_once(
    CONTRACTS,
    '''            "mark_price",
            "position_margin",
''',
    '''            "mark_price",
            "stop_price",
            "position_margin",
''',
)
replace_once(
    CONTRACTS,
    '''        if min(self.quantity, self.average_entry_price, self.mark_price) <= ZERO:
            raise ValueError("position quantity and prices must be positive")
''',
    '''        if min(
            self.quantity,
            self.average_entry_price,
            self.mark_price,
            self.stop_price,
        ) <= ZERO:
            raise ValueError("position quantity and prices must be positive")
        if self.side is PositionSide.LONG and self.stop_price >= self.average_entry_price:
            raise ValueError("long position stop must be below average entry")
        if self.side is PositionSide.SHORT and self.stop_price <= self.average_entry_price:
            raise ValueError("short position stop must be above average entry")
''',
)
replace_once(
    CONTRACTS,
    '''            "mark_price": decimal_string(self.mark_price),
            "position_margin": decimal_string(self.position_margin),
''',
    '''            "mark_price": decimal_string(self.mark_price),
            "stop_price": decimal_string(self.stop_price),
            "position_margin": decimal_string(self.position_margin),
''',
)
replace_once(
    CONTRACTS,
    '''            mark_price=as_decimal(value["mark_price"], "mark_price"),
            position_margin=as_decimal(value["position_margin"], "position_margin"),
''',
    '''            mark_price=as_decimal(value["mark_price"], "mark_price"),
            stop_price=as_decimal(value["stop_price"], "stop_price"),
            position_margin=as_decimal(value["position_margin"], "position_margin"),
''',
)
replace_once(
    CONTRACTS,
    '''    def to_dict(self) -> dict[str, int]:
        return {
            "execution_command_sequence": self.execution_command_sequence,
            "execution_update_sequence": self.execution_update_sequence,
            "market_sequence": self.market_sequence,
            "funding_sequence": self.funding_sequence,
        }


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
''',
    '''    def to_dict(self) -> dict[str, int]:
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
''',
)
replace_once(
    CONTRACTS,
    '''        symbols = [position.symbol for position in self.positions]
        if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
            raise ValueError("snapshot positions must be unique and sorted by symbol")
''',
    '''        symbols = [position.symbol for position in self.positions]
        if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
            raise ValueError("snapshot positions must be unique and sorted by symbol")
        if any(position.account_id != self.account_id for position in self.positions):
            raise ValueError("snapshot contains a position from another account")
        if any(position.updated_at > self.observed_at for position in self.positions):
            raise ValueError("snapshot precedes one of its positions")
''',
)
replace_once(
    CONTRACTS,
    '''    def to_risk_snapshot(self) -> AccountRiskSnapshot:
''',
    '''    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AccountSnapshot:
        snapshot = cls(
            snapshot_id=str(value["snapshot_id"]),
            account_id=str(value["account_id"]),
            observed_at=int(value["observed_at"]),
            balances=AccountBalances.from_dict(dict(value["balances"])),
            positions=tuple(
                PortfolioPosition.from_dict(position)
                for position in value.get("positions", [])
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
                None
                if value.get("storage_sequence") is None
                else int(value["storage_sequence"])
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
''',
)
replace_once(
    CONTRACTS,
    '''                    maintenance_margin_rate=float(position.maintenance_margin_rate),
                    correlation_group=position.correlation_group,
                )
''',
    '''                    maintenance_margin_rate=float(position.maintenance_margin_rate),
                    correlation_group=position.correlation_group,
                    stop_price=float(position.stop_price),
                )
''',
)

replace_once(
    ACCOUNTING,
    '''    if position is None:
        next_position = PortfolioPosition(
''',
    '''    if decision.stop_price is None:
        raise PortfolioAccountingError("approved entry has no stop price")
    stop_price = as_decimal(decision.stop_price, "stop_price")
    if position is None:
        next_position = PortfolioPosition(
''',
)
replace_once(
    ACCOUNTING,
    '''            average_entry_price=delta.price,
            mark_price=delta.price,
            position_margin=margin_allocation,
''',
    '''            average_entry_price=delta.price,
            mark_price=delta.price,
            stop_price=stop_price,
            position_margin=margin_allocation,
''',
)
replace_once(
    ACCOUNTING,
    '''        next_position = replace(
            position,
            quantity=total_quantity,
''',
    '''        if position.stop_price != stop_price:
            raise PortfolioAccountingError("entry fills disagree on the approved stop")
        next_position = replace(
            position,
            quantity=total_quantity,
''',
)

replace_once(
    STORE,
    '''    def list_positions(self, account_id: str) -> list[PortfolioPosition]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT payload FROM account_positions
                WHERE account_id=? ORDER BY symbol""",
                (account_id,),
            ).fetchall()
        return [self._decode_position(str(row["payload"])) for row in rows]
''',
    '''    def _positions_for_account(
        self,
        connection: sqlite3.Connection,
        account_id: str,
    ) -> list[PortfolioPosition]:
        rows = connection.execute(
            """SELECT payload FROM account_positions
            WHERE account_id=? ORDER BY symbol""",
            (account_id,),
        ).fetchall()
        return [self._decode_position(str(row["payload"])) for row in rows]

    def list_positions(self, account_id: str) -> list[PortfolioPosition]:
        with self._connect() as connection:
            return self._positions_for_account(connection, account_id)
''',
)
replace_once(
    STORE,
    '''            positions = self.list_positions(snapshot.account_id)
''',
    '''            positions = self._positions_for_account(connection, snapshot.account_id)
''',
)
replace_once(
    STORE,
    '''        return SnapshotCommit(snapshot.with_storage_sequence(sequence), False)

    def halt_account(self, account_id: str, reason: str, updated_at: int) -> bool:
''',
    '''        return SnapshotCommit(snapshot.with_storage_sequence(sequence), False)

    def load_snapshot(self, snapshot_id: str) -> AccountSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM account_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return AccountSnapshot.from_dict(json.loads(str(row["payload"]))).with_storage_sequence(
            int(row["storage_sequence"])
        )

    def latest_snapshot(self, account_id: str) -> AccountSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM account_snapshots
                WHERE account_id=? ORDER BY storage_sequence DESC LIMIT 1""",
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        return AccountSnapshot.from_dict(json.loads(str(row["payload"]))).with_storage_sequence(
            int(row["storage_sequence"])
        )

    def halt_account(self, account_id: str, reason: str, updated_at: int) -> bool:
''',
)
