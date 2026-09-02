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
        f"expected one Research trading source or existing target, found {count}"
    )


path = Path("src/crypto_quant/research_trading.py")
replace_once(
    path,
    '''from dataclasses import dataclass, field
from decimal import Decimal
''',
    '''from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
''',
)
replace_once(
    path,
    '''    maintenance_margin_rate: Decimal | int | float | str
    correlation_groups: dict[str, str] = field(default_factory=dict)
''',
    '''    maintenance_margin_rate: Decimal | int | float | str
    maintenance_rate_provider: Callable[
        [str, Decimal], Decimal | int | float | str
    ] | None = None
    correlation_groups: dict[str, str] = field(default_factory=dict)
''',
)
replace_once(
    path,
    '''    def _rules(self, symbol: str) -> MarketRules:
''',
    '''    def _maintenance_rate(self, symbol: str, notional: Decimal) -> Decimal:
        selected = (
            self.maintenance_margin_rate
            if self.maintenance_rate_provider is None
            else self.maintenance_rate_provider(symbol, notional)
        )
        rate = as_decimal(selected, "maintenance_margin_rate")
        if not Decimal("0") < rate < Decimal("1"):
            raise ResearchTradeError("invalid research maintenance margin rate")
        return rate

    def _rules(self, symbol: str) -> MarketRules:
''',
)
replace_once(
    path,
    '''            maintenance_margin_rate=self.maintenance_margin_rate,
''',
    '''            maintenance_margin_rate=self._maintenance_rate(
                intent.symbol,
                quantity * selected_execution,
            ),
''',
)
replace_once(
    path,
    '''        selected_fee = (
            self.fee_rate if fee_rate is None else as_decimal(fee_rate, "close fee_rate")
        )
        if exit_intent is None:
''',
    '''        selected_fee = (
            self.fee_rate if fee_rate is None else as_decimal(fee_rate, "close fee_rate")
        )
        if not Decimal("0") <= selected_fee <= Decimal("0.1"):
            raise ValueError("research close fee_rate must be in [0, 0.1]")
        if exit_intent is None:
''',
)
replace_once(
    path,
    '''        entry_plan = self.entry_plans.pop(symbol, None)
        if entry_plan is not None and entry_plan.command is not None:
            self.account.release(
                command_id=entry_plan.command.command_id,
                event_time=event_time,
                reason=f"position_closed:{reason}",
            )
''',
    '''        entry_plan = self.entry_plans.pop(symbol, None)
        if (
            entry_plan is not None
            and entry_plan.command is not None
            and entry_plan.command.command_id != plan.command.command_id
        ):
            self.account.release(
                command_id=entry_plan.command.command_id,
                event_time=event_time,
                reason=f"position_closed:{reason}",
            )
''',
)
