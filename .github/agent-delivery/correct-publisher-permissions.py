"""Apply the pinned shared Issues-read correction to this repository's publishers."""

import subprocess
from pathlib import Path

for source in Path(".github/workflows").glob("*.md"):
    if (
        source.stem == "dependency-maintenance"
        or "environment: gumbot-publisher" not in source.read_text()
    ):
        continue
    subprocess.run(
        [
            "node",
            ".shared-automation/native-toolchain/gh-aw-v0.89.21.cjs",
            str(source),
            str(source.with_suffix(".lock.yml")),
            "correctPublisherIssuesRead",
        ],
        check=True,
    )
