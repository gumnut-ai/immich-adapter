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

# Malformed payload: warn and fail closed (run the checks) — the payload
# producer is Claude Code itself, so malformed means something changed. In
# this bare temp repo the checker deterministically fails, so the fail-closed
# fall-through surfaces as a block (rc 2) with the warning attached.
out=$(printf 'push but not json' | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 2 ] && echo "$out" | grep -q "did not parse as JSON" && ok || fail "adapter: invalid JSON should warn and fail closed (rc=$rc out=$out)"

# "push" on a separate line is a different command, not this git's argument.
out=$(payload "git commit -m msg\necho push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: newline-separated push should no-op (rc=$rc out=$out)"

# Quoted text is data: push mentions inside quotes must not trigger.
out=$(payload "git commit -m 'release notes mention git push'" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: quoted 'git push' mention should no-op (rc=$rc out=$out)"

out=$(payload "git commit -m \\\"pre-push checks\\\"" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: quoted 'pre-push checks' should no-op (rc=$rc out=$out)"

# push embedded in a filename token is not the push subcommand.
out=$(payload "git add scripts/pre-push-checks_test.sh && git commit -m msg" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: pre-push filename should no-op (rc=$rc out=$out)"

# --- adapter: inline skip flag (command-position assignment) ---
out=$(payload "GUMNUT_SKIP_PUSH_CHECKS=1 git push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter: inline skip flag should skip"

# A QUOTED mention of the skip flag is data, not an assignment — the checker
# must still run (its deterministic failure here proves it; the
# accidental-bypass case).
out=$(payload "git commit -m 'document GUMNUT_SKIP_PUSH_CHECKS=1' && git push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 2 ] && echo "$out" | grep -q "FAILED: immich-version-sync" && ok || fail "adapter: quoted skip-flag mention must NOT bypass the checker (rc=$rc out=$out)"

# An UNQUOTED mention in argument position is not an assignment either.
out=$(payload "grep -r GUMNUT_SKIP_PUSH_CHECKS=1 docs && git push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 2 ] && echo "$out" | grep -q "FAILED: immich-version-sync" && ok || fail "adapter: argument-position skip-flag text must NOT bypass (rc=$rc out=$out)"

# An assignment scoped to a DIFFERENT command does not skip the push.
out=$(payload "GUMNUT_SKIP_PUSH_CHECKS=1 true && git push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 2 ] && echo "$out" | grep -q "FAILED: immich-version-sync" && ok || fail "adapter: assignment on another command must NOT skip (rc=$rc out=$out)"

# Partial skip: an unskipped push in the chain still runs the checks.
out=$(payload "GUMNUT_SKIP_PUSH_CHECKS=1 git push && git push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 2 ] && echo "$out" | grep -q "FAILED: immich-version-sync" && ok || fail "adapter: partial skip must still run checks (rc=$rc out=$out)"

# Escaped quotes inside a double-quoted message stay masked.
out=$(payload 'git commit -m \"release \\\"git push\\\"\"' | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: escaped quotes in a message must stay masked (rc=$rc out=$out)"

# A quoted SUBCOMMAND still executes a push after quote removal — detected.
out=$(payload 'git \"push\"' | GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter: git \"push\" quoted subcommand must be detected (rc=$rc out=$out)"

# git not in command position is not a push.
out=$(payload "printf git push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: printf git push must not match (rc=$rc out=$out)"

# Unquoted push must also be in subcommand position.
out=$(payload "git grep push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: git grep push must not match (rc=$rc out=$out)"

out=$(payload "git log --grep=push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: git log --grep=push must not match (rc=$rc out=$out)"

# Brace groups are command positions.
out=$(payload "{ git push; }" | GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter: { git push; } must be detected (rc=$rc out=$out)"

# A nested shell script argument is command context, not data.
out=$(payload "bash -lc 'git push'" | GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter: bash -lc 'git push' must be detected (rc=$rc out=$out)"

# A timed push is still a push.
out=$(payload "time git push" | GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter: time git push must be detected (rc=$rc out=$out)"

# A negated push still executes the push.
out=$(payload "if ! git push; then echo failed; fi" | GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter: ! git push must be detected (rc=$rc out=$out)"

# Heredoc bodies are data.
out=$(payload "cat >doc.md <<'EOF'\ngit push\nEOF" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: heredoc body mentioning git push must no-op (rc=$rc out=$out)"

# The skip flag must be its own assignment — a mention inside another
# assignment's VALUE does not skip (the checker runs).
out=$(payload "FOO=GUMNUT_SKIP_PUSH_CHECKS=1 git push" | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 2 ] && echo "$out" | grep -q "FAILED: immich-version-sync" && ok || fail "adapter: skip text inside another assignment value must NOT skip (rc=$rc out=$out)"

# Shell control-structure bodies are command positions.
out=$(payload "if true; then git push; fi" | GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter: if/then git push must be detected (rc=$rc out=$out)"

# Quoted-push fallback is subcommand-position only: a quoted "push"
# ARGUMENT to a non-push git command is data.
out=$(payload 'git grep \"push\"' | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: git grep \"push\" must not match (rc=$rc out=$out)"

out=$(payload 'git commit -m \"push\"' | "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok || fail "adapter: git commit -m \"push\" must not match (rc=$rc out=$out)"

# An unterminated quote (apostrophe in prose) falls back to raw matching —
# a real push after it must still be detected (never fail open).
out=$(payload "echo don't forget >> notes.txt\ngit push" | GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter: unterminated quote must not mask a real push (rc=$rc out=$out)"

# A double-quoted command substitution really pushes — must be detected.
out=$(payload "out=\\\"\$(git push origin HEAD 2>&1)\\\"" | GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter: \"\$(git push)\" substitution must be detected (rc=$rc out=$out)"

# --- adapter: push detection positives (exported skip keeps it hermetic;
# the checker's "skipped" message proves the adapter reached it) ---
for cmd in "git push" "git push -u origin HEAD" "git -C /tmp/x push" "git add -A && git commit -m msg && git push" "git stash push -m wip && git push" "/usr/bin/git push"; do
  out=$(payload "$cmd" | GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
  [ "$rc" -eq 0 ] && echo "$out" | grep -q "skipped" && ok || fail "adapter should detect push in: $cmd"
done

# --- adapter: fail closed when python3 is broken (exit 127 AND exit 1 — a
# broken shim or a syntax error in the embedded program exits 1, which must
# never be conflated with "not a push") ---
for stub_rc in 127 1; do
  STUBBIN=$(mktemp -d "${TMPDIR:-/tmp}/prepushstub.XXXXXX")
  printf '#!/usr/bin/env bash\nexit %s\n' "$stub_rc" >"$STUBBIN/python3"
  chmod +x "$STUBBIN/python3"
  out=$(payload "git push" | PATH="$STUBBIN:$PATH" GUMNUT_SKIP_PUSH_CHECKS=1 "$ADAPTER" 2>&1); rc=$?
  rm -rf "$STUBBIN"
  [ "$rc" -eq 0 ] && ok || fail "fail-closed path (python3 exit $stub_rc) should still honor the checker's skip (rc=$rc)"
  echo "$out" | grep -q "fail closed" && ok || fail "python3 exiting $stub_rc should emit the fail-closed warning"
  echo "$out" | grep -q "skipped" && ok || fail "fail-closed path (python3 exit $stub_rc) should reach the checker"
done

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

# --- checker: clone paths containing quotes or $ must not break the group
# scripts (repo root is passed unexpanded via env, expanded once inside) ---
EXOTIC="$REPO/o'con\$nor"
mkdir -p "$EXOTIC/repo-e"
git -C "$EXOTIC/repo-e" -c init.defaultBranch=main init -q
git -C "$EXOTIC/repo-e" config user.email t@example.com
git -C "$EXOTIC/repo-e" config user.name T
echo "v1.2.3" >"$EXOTIC/repo-e/.immich-container-tag"
printf 'ARG IMMICH_VERSION=v1.2.3\nFROM scratch\n' >"$EXOTIC/repo-e/Dockerfile"
git -C "$EXOTIC/repo-e" add -A && git -C "$EXOTIC/repo-e" commit -qm base
STUBBIN=$(mktemp -d "${TMPDIR:-/tmp}/prepushstub.XXXXXX")
printf '#!/usr/bin/env bash\nexit 0\n' >"$STUBBIN/uv"
chmod +x "$STUBBIN/uv"
out=$(cd "$EXOTIC/repo-e" && PATH="$STUBBIN:$PATH" "$CHECKER" 2>&1); rc=$?
rm -rf "$STUBBIN"
[ "$rc" -eq 0 ] && echo "$out" | grep -q "all checks passed" && ok || fail "checker must pass from a quote/dollar clone path (rc=$rc out=$out)"

echo ""
if [ "$failed" -gt 0 ]; then
  echo "pre-push-checks_test: ${passed} passed, ${failed} FAILED" >&2
  exit 1
fi
echo "pre-push-checks_test: ${passed} passed"
