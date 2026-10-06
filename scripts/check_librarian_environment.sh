#!/usr/bin/env bash
# Run inside the librarian's default AWF sandbox after the host has prepared uv.
set -euo pipefail

for tool in git gh rg python uv; do
  command -v "$tool" >/dev/null || {
    echo "librarian environment: missing $tool in sandbox PATH" >&2
    exit 1
  }
done

test "$(git rev-parse --show-toplevel)" = "$PWD"
test "$(git rev-parse --is-shallow-repository)" = false
git rev-parse --verify --quiet 'origin/main^{commit}' >/dev/null
git merge-base HEAD origin/main >/dev/null
git ls-files --error-unmatch scripts/lint_docs.py scripts/lint_docs.py.lock >/dev/null

python - <<'PY'
from pathlib import Path
from platform import python_version

requested = Path(".python-version").read_text().strip()
actual = python_version()
if actual != requested and not actual.startswith(f"{requested}."):
    raise SystemExit(f"librarian environment: Python {actual} does not match {requested}")
print(f"python={actual}")
PY

expected_uv=$(cat .uv-version)
actual_uv=$(uv --version | awk '{print $2}')
test "$actual_uv" = "$expected_uv" || {
  echo "librarian environment: uv $actual_uv does not match $expected_uv" >&2
  exit 1
}
echo "uv=$actual_uv"
echo "git=$(git --version)"
echo "gh=$(gh --version | head -n 1)"
echo "rg=$(rg --version | head -n 1)"
echo "origin_main=$(git rev-parse origin/main)"
echo "merge_base=$(git merge-base HEAD origin/main)"

# Preparation must have populated the standalone script environment already.
# Offline mode makes an accidental package download fail in the agent sandbox.
UV_OFFLINE=1 uv run --no-config --locked --script scripts/lint_docs.py --base origin/main
