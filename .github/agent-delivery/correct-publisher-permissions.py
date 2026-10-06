"""Restore Issues read for gh-aw 0.89.21 publisher label checks after compilation."""

import re
from pathlib import Path

for source in Path(".github/workflows").glob("*.md"):
    if "environment: gumbot-publisher" not in source.read_text():
        continue
    path = source.with_suffix(".lock.yml")
    text = path.read_text()
    pattern = r"(?ms)(      - name: Generate GitHub App token\n        id: safe-outputs-app-token\n.*?)(?=      - name:|\Z)"
    blocks = list(re.finditer(pattern, text))
    assert len(blocks) == 2, f"{path}: pinned publisher token steps changed"
    for match in reversed(blocks):
        block = match[0]
        anchor = "          permission-pull-requests: write\n"
        assert block.count(anchor) == 1, f"{path}: publisher permissions changed"
        if "          permission-issues: read\n" not in block:
            block = block.replace(
                anchor, anchor + "          permission-issues: read\n"
            )
        text = text[: match.start()] + block + text[match.end() :]
    path.write_text(text)
