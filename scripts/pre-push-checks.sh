#!/usr/bin/env bash
# scripts/pre-push-checks.sh
#
# Run the fast subset of CI before pushing, so failures surface locally
# instead of burning a CI round. Platform-neutral: any agent (or human) runs
# it directly before pushing or finishing a task. Claude Code runs it
# automatically on every `git push` via the `.claude/hooks/pre-push-checks.sh`
# PreToolUse adapter.
#
# What runs (mirroring ci.yml's lint-and-typecheck and version-sync jobs):
#   - uv run ruff check
#   - uv run ruff format --check
#   - uv run pyright
#   - .immich-container-tag ↔ Dockerfile IMMICH_VERSION match
# Deliberate exclusions: pytest (the slow part; CI covers it) and the API
# compatibility workflow (fetches the Immich spec — CI-only).
#
# The checks validate the *working tree*; CI validates the pushed commits.
# This repo is a single project, so there is no changed-file scoping — the
# full set runs on every push (~4s warm).
#
# Escape hatch (emergencies only): GUMNUT_SKIP_PUSH_CHECKS=1
#
# Exit codes: 0 — all checks passed (or skipped); 1 — a check failed.

set -uo pipefail

if [ "${GUMNUT_SKIP_PUSH_CHECKS:-0}" = "1" ]; then
  echo "pre-push-checks: skipped (GUMNUT_SKIP_PUSH_CHECKS=1)" >&2
  exit 0
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "pre-push-checks: not inside a git repository" >&2
  exit 1
}
cd "$repo_root" || exit 1

tmpdir=$(mktemp -d) || exit 1
trap 'rm -rf "$tmpdir"' EXIT

declare -a check_names=()

# run_check <name> <script>: run one check in the background, capturing
# combined output and exit code. `uv run` self-syncs the environment.
run_check() {
  local name="$1" script="$2"
  (
    bash -c "$script" >"$tmpdir/$name.out" 2>&1
    echo $? >"$tmpdir/$name.code"
  ) &
  check_names+=("$name")
}

# --locked mirrors CI's `uv sync --locked`: a stale uv.lock must fail here
# too (bare `uv run` would silently re-lock it and green-light a push that
# CI then rejects).
run_check "ruff-check" "cd '$repo_root' && uv run --locked ruff check"
run_check "ruff-format" "cd '$repo_root' && uv run --locked ruff format --check"
run_check "pyright" "cd '$repo_root' && uv run --locked pyright"
# Mirrors ci.yml's check-immich-version-sync job; see
# docs/references/code-practices.md § "Bumping the Immich Version".
run_check "immich-version-sync" "
  cd '$repo_root' &&
  TAG=\$(cat .immich-container-tag) &&
  DOCKERFILE_VERSION=\$(grep -E '^ARG IMMICH_VERSION=' Dockerfile | cut -d= -f2) &&
  if [ \"\$TAG\" != \"\$DOCKERFILE_VERSION\" ]; then
    echo \"Mismatch: .immich-container-tag=\$TAG but Dockerfile ARG IMMICH_VERSION=\$DOCKERFILE_VERSION.\"
    echo \"See docs/references/code-practices.md § 'Bumping the Immich Version'.\"
    exit 1
  fi"

wait

failed=0
for name in "${check_names[@]}"; do
  code=$(cat "$tmpdir/$name.code" 2>/dev/null || echo 1)
  if [ "$code" != "0" ]; then
    failed=1
    {
      echo "=== pre-push check FAILED: $name (exit $code) ==="
      cat "$tmpdir/$name.out" 2>/dev/null
      echo
    } >&2
  fi
done

if [ "$failed" = "1" ]; then
  {
    echo "pre-push-checks: fix the failures above before pushing."
    echo "(These are the same checks CI runs; pushing now would fail CI.)"
  } >&2
  exit 1
fi

echo "pre-push-checks: all checks passed (${check_names[*]})" >&2
exit 0
