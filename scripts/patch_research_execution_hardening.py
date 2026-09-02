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
        f"expected one Research execution source or existing target, found {count}"
    )


path = Path("src/crypto_quant/research_execution.py")
replace_once(
    path,
    '''        if account.account_id == "":
            raise ValueError("research account identity must not be empty")
''',
    '''        if not account.account_id:
            raise ValueError("research account identity must not be empty")
''',
)
replace_once(
    path,
    '''        equity = account.equity
        day_start = equity if day_start_equity is None else as_decimal(
            day_start_equity,
            "day_start_equity",
        )
        peak = max(
            equity,
            equity if peak_equity is None else as_decimal(peak_equity, "peak_equity"),
        )
''',
    '''        equity = account.equity
        day_start = (
            max(Decimal("0"), equity)
            if day_start_equity is None
            else as_decimal(day_start_equity, "day_start_equity")
        )
        peak = max(
            Decimal("0"),
            equity,
            equity if peak_equity is None else as_decimal(peak_equity, "peak_equity"),
        )
''',
)
replace_once(
    path,
    '''            positions=tuple(account.positions.values()),
''',
    '''            positions=tuple(
                account.positions[symbol] for symbol in sorted(account.positions)
            ),
''',
)
replace_once(
    path,
    '''            bid_price=(float(risk_mark) if buy else float(selected_execution)),
            ask_price=(float(selected_execution) if buy else float(risk_mark)),
''',
    '''            bid_price=None if buy else float(selected_execution),
            ask_price=float(selected_execution) if buy else None,
''',
)
replace_once(
    path,
    '''        if quantity == 0:
            if quote != 0:
                raise ValueError("zero research fill cannot contain quote value")
            state = OrderState.ACKNOWLEDGED
            average = None
        else:
            if quote <= 0:
                raise ValueError("positive research fill requires quote value")
            complete = abs(quantity - requested) <= Decimal("0.000000000001")
            state = OrderState.FILLED if complete else OrderState.PARTIALLY_FILLED
            average = quote / quantity
''',
    '''        if quantity <= 0 or quote <= 0:
            raise ValueError("research fill requires positive quantity and quote value")
        complete = abs(quantity - requested) <= Decimal("0.000000000001")
        state = OrderState.FILLED if complete else OrderState.PARTIALLY_FILLED
        average = quote / quantity
''',
)
replace_once(
    path,
    '''        digest = self._fingerprint(payload)
        return OrderUpdate(
''',
    '''        digest = self._fingerprint(payload)
        broker_digest = hashlib.blake2s(order.order_id.encode(), digest_size=12).hexdigest()
        return OrderUpdate(
''',
)
replace_once(
    path,
    '''            broker_order_id="research-" + digest[:24],
''',
    '''            broker_order_id="research-" + broker_digest,
''',
)
