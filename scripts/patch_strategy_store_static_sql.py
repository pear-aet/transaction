from pathlib import Path

path = Path("src/crypto_quant/strategy_store.py")
text = path.read_text(encoding="utf-8")

replacements = (
    (
        '''        query = "SELECT * FROM strategy_intents WHERE delivery_state='pending'"
        parameters: list[Any] = []
        if instance_id is not None:
            query += " AND instance_id=?"
            parameters.append(instance_id)
        query += " ORDER BY storage_sequence LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
''',
        '''        query = """SELECT * FROM strategy_intents
        WHERE delivery_state='pending'
        AND (? IS NULL OR instance_id=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (instance_id, instance_id, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
''',
    ),
    (
        '''        clauses = ["storage_sequence>?"]
        parameters: list[Any] = [after_sequence]
        if through_sequence is not None:
            if through_sequence < after_sequence:
                return []
            clauses.append("storage_sequence<=?")
            parameters.append(through_sequence)
        if instance_id is not None:
            clauses.append("instance_id=?")
            parameters.append(instance_id)
        if symbol is not None:
            clauses.append("symbol=?")
            parameters.append(symbol)
        query = "SELECT * FROM strategy_intents WHERE " + " AND ".join(clauses)
        query += " ORDER BY storage_sequence LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
''',
        '''        if through_sequence is not None and through_sequence < after_sequence:
            return []
        query = """SELECT * FROM strategy_intents
        WHERE storage_sequence>?
        AND (? IS NULL OR storage_sequence<=?)
        AND (? IS NULL OR instance_id=?)
        AND (? IS NULL OR symbol=?)
        ORDER BY storage_sequence LIMIT ?"""
        parameters = (
            after_sequence,
            through_sequence,
            through_sequence,
            instance_id,
            instance_id,
            symbol,
            symbol,
            limit,
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
''',
    ),
    (
        '''        clauses: list[str] = []
        parameters: list[Any] = []
        if instance_id is not None:
            clauses.append("instance_id=?")
            parameters.append(instance_id)
        if symbol is not None:
            clauses.append("symbol=?")
            parameters.append(symbol)
        query = "SELECT * FROM strategy_intents"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY storage_sequence DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
''',
        '''        query = """SELECT * FROM strategy_intents
        WHERE (? IS NULL OR instance_id=?)
        AND (? IS NULL OR symbol=?)
        ORDER BY storage_sequence DESC LIMIT ?"""
        parameters = (instance_id, instance_id, symbol, symbol, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
''',
    ),
)

for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one SQL block, found {count}")
    text = text.replace(old, new)

path.write_text(text, encoding="utf-8")
