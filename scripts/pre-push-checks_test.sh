#!/usr/bin/env bash
# scripts/pre-push-checks_test.sh
#
# Tests for the Claude Code pre-push adapter (.claude/hooks/pre-push-checks.sh)
# and the checker's skip/failure contract. Uses a throwaway git repo; cases
# that would reach the real toolchain short-circuit via the exported
# GUMNUT_SKIP_PUSH_CHECKS flag, keeping the suite hermetic (bash + git +
# python3 only). Run: scripts/pre-push-checks_test.sh

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CHECKER="$SCRIPT_DIR/pre-push-checks.sh"
ADAPTER="$SCRIPT_DIR/../.claude/hooks/pre-push-checks.sh"

passed=0
failed=0

fail() {
  echo "  FAIL: $1" >&2
  failed=$((failed + 1))
}
ok() {
  passed=$((passed + 1))
}

REPO=$(mktemp -d "${TMPDIR:-/tmp}/prepushtest.XXXXXX")
trap 'rm -rf "$REPO"' EXIT
cd "$REPO" || exit 1
git -c init.defaultBranch=main init -q
git config user.email test@example.com
git config user.name "Test"
echo base >README.md
git add -A && git commit -qm baseline

payload() { printf '{"tool_input":{"command":"%s"}}' "$1"; }

# --- checker: skip flag ---
out=$(GUMNUT_SKIP_PUSH_CHECKS=1 "$CHECKER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "checker should skip on GUMNUT_SKIP_PUSH_CHECKS=1 (rc=$rc)"

# --- adapter: non-push commands no-op silently ---
out=$(payload "ls -la" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: non-push command should no-op (rc=$rc out=$out)"

out=$(payload "git status" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: git non-push should no-op"

out=$(payload "git stash push -m wip" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: git stash push should no-op (rc=$rc out=$out)"

out=$(printf 'push but not json' | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok || fail "adapter: invalid JSON should no-op"

# --- adapter: inline skip flag ---
out=$(payload "GUMNUT_SKIP_PUSH_CHECKS=1 git push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter: inline skip flag should skip"

# --- adapter: push detection positives (exported skip keeps it hermetic;
# the checker's "skipped" message proves the adapter reached it) ---
for cmd in "git push" "git push -u origin HEAD" "git -C /tmp/x push" "git add -A && git commit -m msg && git push"; do
  out=$(payload "$cmd" | GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
  [ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter should detect push in: $cmd"
done

# --- adapter: fail closed when python3 is broken ---
STUBBIN=$(mktemp -d "${TMPDIR:-/tmp}/prepushstub.XXXXXX")
printf '#!/usr/bin/env bash\nexit 127\n' >"$STUBBIN/python3"
chmod +x "$STUBBIN/python3"
out=$(payload "git push" | PATH="$STUBBIN:$PATH" GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
rm -rf "$STUBBIN"
[ "$rc" -eq 0 ] && ok || fail "fail-closed path should still honor the checker's skip (rc=$rc)"
echo "$out" | grep -q "fail closed" && ok || fail "broken python3 should emit the fail-closed warning"
echo "$out" | grep -q "skipped" && ok || fail "fail-closed path should reach the checker"

# --- adapter: failure mapping (checker exit 1 -> adapter exit 2) ---
# In this bare temp repo the checker deterministically fails (no
# .immich-container-tag, no project for the uv checks), which exercises the
# failure path end-to-end without needing the real toolchain to succeed.
out=$(payload "git push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 2 ] && ok || fail "adapter should map checker failure to exit 2 (rc=$rc)"
echo "$out" | grep -q "FAILED: immich-version-sync" && ok || fail "failure report should name the failing check"
echo "$out" | grep -q "fix the failures above" && ok || fail "failure report should include the remediation trailer"

# --- checker: hermetic happy path (stub uv + matching version files) ---
# NOTE: cases from here down run in a *populated* repo (version files
# written below); earlier cases depend on the bare repo, so new bare-repo
# cases must go above this line.
# A stub uv that succeeds silently (logging its args), plus a matching
# tag/Dockerfile pair, drives every check green: rc=0 and the pass message
# lists all four.
echo "v1.2.3" >.immich-container-tag
printf 'ARG IMMICH_VERSION=v1.2.3\nFROM scratch\n' >Dockerfile
STUBBIN=$(mktemp -d "${TMPDIR:-/tmp}/prepushstub.XXXXXX")
printf '#!/usr/bin/env bash\necho "$*" >>"%s/calls"\nexit 0\n' "$STUBBIN" >"$STUBBIN/uv"
chmod +x "$STUBBIN/uv"
out=$(PATH="$STUBBIN:$PATH" "$CHECKER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok || fail "checker should exit 0 when all checks pass (rc=$rc)"
echo "$out" | grep -q "all checks passed (ruff-check ruff-format pyright immich-version-sync)" && ok || fail "pass message should list all four checks"
# CI-parity guard: every uv invocation must carry --locked (bare `uv run`
# silently re-locks a stale uv.lock that CI's `uv sync --locked` rejects).
[ "$(grep -c -- '--locked' "$STUBBIN/calls" 2>/dev/null)" -eq 3 ] && ok || fail "all three uv invocations should carry --locked"

# --- checker: version-sync mismatch fails even when the uv checks pass ---
printf 'ARG IMMICH_VERSION=v9.9.9\nFROM scratch\n' >Dockerfile
out=$(PATH="$STUBBIN:$PATH" "$CHECKER" 2>&1); rc=$?
rm -rf "$STUBBIN"
[ "$rc" -eq 1 ] && ok || fail "version mismatch should fail the checker (rc=$rc)"
echo "$out" | grep -q "FAILED: immich-version-sync" && ok || fail "version mismatch should be reported"
echo "$out" | grep -q "Mismatch: .immich-container-tag=v1.2.3" && ok || fail "mismatch message should show both values"
rm -f .immich-container-tag Dockerfile

echo ""
if [ "$failed" -gt 0 ]; then
  echo "pre-push-checks_test: ${passed} passed, ${failed} FAILED" >&2
  exit 1
fi
echo "pre-push-checks_test: ${passed} passed"
