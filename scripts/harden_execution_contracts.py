from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/crypto_quant/execution_contracts.py",
    '''        _strict_json(self.evidence or {})
        json.dumps(self.evidence or {}, sort_keys=True, allow_nan=False)

    @property
''',
    '''        _strict_json(self.evidence or {})
        json.dumps(self.evidence or {}, sort_keys=True, allow_nan=False)
        if (self.evidence or {}).get("submitted") is not False:
            raise ValueError("shadow/simulation order must remain submitted=false")
        if (self.evidence or {}).get("network_write") is not False:
            raise ValueError("shadow/simulation order cannot permit network writes")

    @property
''',
)
replace_once(
    "src/crypto_quant/execution_contracts.py",
    '''    def with_storage_sequence(self, sequence: int) -> ExecutionOrder:
        if sequence <= 0:
            raise ValueError("order storage sequence must be positive")
        return replace(self, storage_sequence=sequence)

    def to_dict(self) -> dict[str, Any]:
''',
    '''    def with_storage_sequence(self, sequence: int) -> ExecutionOrder:
        if sequence <= 0:
            raise ValueError("order storage sequence must be positive")
        return replace(self, storage_sequence=sequence)

    def plan_dict(self) -> dict[str, Any]:
        return finite_json(
            {
                "order_id": self.order_id,
                "command_id": self.command_id,
                "client_order_id": self.client_order_id,
                "leg_type": self.leg_type.value,
                "side": self.side.value,
                "order_type": self.order_type.value,
                "quantity": self.quantity,
                "reduce_only": self.reduce_only,
                "trigger_price": self.trigger_price,
                "activation_order_id": self.activation_order_id,
                "evidence": self.evidence or {},
            }
        )

    def to_dict(self) -> dict[str, Any]:
''',
)
replace_once(
    "src/crypto_quant/execution_contracts.py",
    '''        _strict_json(self.evidence)
        json.dumps(self.evidence, sort_keys=True, allow_nan=False)

    def with_storage_sequence(self, sequence: int) -> ExecutionCommand:
        if sequence <= 0:
            raise ValueError("command storage sequence must be positive")
        orders = tuple(order.with_storage_sequence(order.storage_sequence or sequence) for order in self.orders)
        return replace(self, storage_sequence=sequence, orders=orders)
''',
    '''        _strict_json(self.evidence)
        json.dumps(self.evidence, sort_keys=True, allow_nan=False)
        if self.evidence.get("submitted") is not False:
            raise ValueError("execution command must remain submitted=false")
        if self.evidence.get("network_write") is not False:
            raise ValueError("execution command cannot permit network writes")

    def with_storage_sequence(self, sequence: int) -> ExecutionCommand:
        if sequence <= 0:
            raise ValueError("command storage sequence must be positive")
        return replace(self, storage_sequence=sequence)
''',
)
replace_once(
    "src/crypto_quant/execution_contracts.py",
    '                "orders": [order.to_dict() for order in self.orders],\n',
    '                "orders": [order.plan_dict() for order in self.orders],\n',
)
replace_once(
    "src/crypto_quant/execution_contracts.py",
    '''        if self.cumulative_filled_quantity > 0:
            if self.average_fill_price is None or self.average_fill_price <= 0:
                raise ValueError("filled update requires an average fill price")
        elif self.average_fill_price is not None or self.cumulative_quote_value != 0:
''',
    '''        if self.cumulative_filled_quantity > 0:
            if self.average_fill_price is None or self.average_fill_price <= 0:
                raise ValueError("filled update requires an average fill price")
            expected_quote = self.cumulative_filled_quantity * self.average_fill_price
            if not math.isclose(
                self.cumulative_quote_value,
                expected_quote,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                raise ValueError("order update quote value is inconsistent with average fill price")
        elif self.average_fill_price is not None or self.cumulative_quote_value != 0:
''',
)

replace_once(
    "src/crypto_quant/execution.py",
    '''        reduce_only=False,
        orders=(),
''',
    '''        reduce_only=context.decision.reduce_only,
        orders=(),
''',
)
replace_once(
    "src/crypto_quant/execution.py",
    '''    if update.cumulative_filled_quantity > order.quantity + 1e-12:
        raise OrderTransitionError("filled quantity exceeds requested quantity")
''',
    '''    if update.cumulative_filled_quantity > order.quantity + 1e-12:
        raise OrderTransitionError("filled quantity exceeds requested quantity")
    if update.new_state is OrderState.PARTIALLY_FILLED:
        if update.cumulative_filled_quantity <= order.cumulative_filled_quantity + 1e-12:
            raise OrderTransitionError("partial-fill update must increase filled quantity")
''',
)
