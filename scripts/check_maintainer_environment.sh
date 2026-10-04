#!/usr/bin/env bash
# Execute only inside the actual AWF profile, including the full required suites.
set -euo pipefail
mode=${1:?smoke or candidate}
for tool in git gh rg python uv node; do
  command -v "$tool" >/dev/null || { echo "missing sandbox tool: $tool" >&2; exit 1; }
done
test "$(git rev-parse --is-shallow-repository)" = false
test "$(uv --version | awk '{print $2}')" = "$(cat .uv-version)"
python - <<'PY'
from pathlib import Path
import platform
requested = Path('.python-version').read_text().strip()
actual = platform.python_version()
if actual != requested and not actual.startswith(requested + '.'):
    raise SystemExit(f'Python {actual} differs from {requested}')
print(f'python={actual}')
PY
echo "sandbox checks begin: mode=$mode source=${GITHUB_SHA:-unset}"
python_checks() {
  (
    cd .
    uv sync --locked --all-extras --dev
    uv run python - <<'PY_SERVICE'
import redis
if not redis.Redis(host='host.docker.internal', port=6379).ping():
    raise SystemExit('sandbox cannot reach disposable Redis')
print('disposable Redis reachable from sandbox')
PY_SERVICE
    uv run ruff format --check
    uv run ruff check
    uv run pyright
    uv run --no-config --locked --script scripts/lint_docs.py --base "$GITHUB_SHA"
    REDIS_URL=redis://host.docker.internal:6379/1 uv run pytest
  )
}
case "$mode" in
  smoke)
    python_checks
    ;;
  candidate)
    test -n "${GITHUB_SHA:-}"
    paths=$(git diff --name-only "$GITHUB_SHA" HEAD)
    if [[ -z "$paths" ]]; then echo 'clean source revision; smoke already checked'; exit 0; fi
    python_checks
    ;;
  *) echo 'expected smoke or candidate' >&2; exit 1 ;;
esac
echo 'full required sandbox checks passed'
