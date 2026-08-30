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
