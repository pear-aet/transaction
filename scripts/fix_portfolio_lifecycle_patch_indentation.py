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
