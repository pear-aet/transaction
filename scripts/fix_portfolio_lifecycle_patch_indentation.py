from pathlib import Path

path = Path("scripts/patch_portfolio_lifecycle.py")
text = path.read_text(encoding="utf-8")
replacements = {
    'file.write_text(text.rstrip() + "\\n\\n" + content.lstrip(), encoding="utf-8")': (
        'file.write_text(text.rstrip() + "\\n\\n" + content.lstrip("\\n"), encoding="utf-8")'
    ),
    "'''def commit_mark(": "'''    def commit_mark(",
    "'''def load_commands(": "'''    def load_commands(",
}
for old, new in replacements.items():
    count = text.count(old)
    if count == 1:
        text = text.replace(old, new)
    elif count == 0 and new in text:
        continue
    else:
        raise SystemExit(
            f"expected one lifecycle patch source or existing target for {old!r}, "
            f"found {count}"
        )
path.write_text(text, encoding="utf-8")

round4 = Path("scripts/patch_portfolio_lifecycle_round4.py")
round4_text = round4.read_text(encoding="utf-8")
old = '''tail = tail.replace(
    \'''            mark_price=delta.price,
            position_margin=position.position_margin - margin_release,
            initial_risk=position.initial_risk - risk_release,
            updated_at=update.event_time,
\''',
    \'''            mark_price=(
                position.mark_price
                if position.last_mark_time is not None
                and position.last_mark_time >= update.event_time
                else delta.price
            ),
            position_margin=position.position_margin - margin_release,
            initial_risk=position.initial_risk - risk_release,
            last_execution_time=update.event_time,
            updated_at=max(position.updated_at, update.event_time),
\''',
)
'''
new = '''tail = tail.replace(
    "            mark_price=delta.price,\\n",
    \'''            mark_price=(
                position.mark_price
                if position.last_mark_time is not None
                and position.last_mark_time >= update.event_time
                else delta.price
            ),
\''',
)
tail = tail.replace(
    "            updated_at=update.event_time,\\n",
    \'''            last_execution_time=update.event_time,
            updated_at=max(position.updated_at, update.event_time),
\''',
)
'''
count = round4_text.count(old)
if count == 1:
    round4.write_text(round4_text.replace(old, new), encoding="utf-8")
elif count == 0 and new in round4_text:
    pass
else:
    raise SystemExit(
        f"expected one close-fill time-axis source or existing target, found {count}"
    )

finalization = Path("scripts/patch_portfolio_runtime_audit_propagation.py")
finalization_text = finalization.read_text(encoding="utf-8")
old = '''            and position.last_mark_time > event_time
            else selected_mark
'''
new = '''            and position.last_mark_time >= event_time
            else selected_mark
'''
count = finalization_text.count(old)
if count == 1:
    finalization.write_text(finalization_text.replace(old, new), encoding="utf-8")
elif count == 0 and new in finalization_text:
    pass
else:
    raise SystemExit(
        f"expected one funding/mark priority source or existing target, found {count}"
    )
