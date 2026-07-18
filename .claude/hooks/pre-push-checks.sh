#!/usr/bin/env bash
# .claude/hooks/pre-push-checks.sh
#
# Claude Code adapter for scripts/pre-push-checks.sh (the platform-neutral
# checker — all check logic lives there). Wired in `.claude/settings.json` as
# a PreToolUse hook on the Bash tool, so it fires for EVERY Bash command:
# this adapter reads the tool-call JSON on stdin and exits 0 immediately
# unless the command is a `git push`. Because it intercepts the tool call
# itself, `git push --no-verify` does not bypass it (that flag only skips
# `.git/hooks/*`). Runs in local Claude Code and in claude.ai/code cloud
# sessions alike — the settings file ships with the clone.
#
# Exit codes (Claude Code hook contract):
#   0 — allow the push (not a push command, checks passed, or skipped)
#   2 — block the push; stderr is fed back to Claude to fix and retry

set -uo pipefail

# This adapter runs for EVERY Bash tool call, so the common path must be
# cheap: a raw substring pre-filter decides whether the payload could even
# mention a push before paying the ~80ms python3 spawn for real JSON parsing.
payload=$(cat)
case "$payload" in
  *push*) ;;
  *) exit 0 ;;
esac

# Stdin carried the PreToolUse payload: {"tool_input": {"command": "..."}, ...}.
# python3 does the JSON parsing AND the push matching: unlike jq it's present
# on every macOS/Linux dev and cloud image this repo targets, and unlike
# sed/grep its \b regex semantics don't vary between BSD and GNU tools.
#
# Match `git push` allowing intervening flags (`git -C x push`) and compound
# commands (`git add ... && git push`), but not `git stash push` — a stash is
# what an agent reaches for when the tree is dirty, i.e. exactly when checks
# would fail, so a false match there would block an unrelated command.
# Remaining over-matches cost a needless check run when checks pass but DO
# block the command when checks fail — notably a push targeting a *different*
# repo from a session in this one (`git -C ../other-repo push`), which this
# repo's failures would block with confusing errors; the
# GUMNUT_SKIP_PUSH_CHECKS=1 escape hatch is the remedy. Under-matching would
# let a real push through unchecked, so still err broad.
#
# python3 exit codes: 0 = push detected, 1 = not a push. Anything else means
# python3 itself is missing/broken — treat that as push-detected (fail
# closed, per the err-broad posture) rather than silently disabling the
# guard forever on that environment.
printf '%s' "$payload" | python3 -c '
import json, re, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)
cmd = payload.get("tool_input", {}).get("command", "")
cmd = re.sub(r"\bstash\s+push\b", "", cmd)
sys.exit(0 if re.search(r"\bgit\b[^|;&]*\bpush\b", cmd) else 1)
'
rc=$?
if [ "$rc" = "1" ]; then
  exit 0
elif [ "$rc" != "0" ]; then
  echo "pre-push-checks adapter: python3 failed (exit $rc); treating command as a push and running checks (fail closed)" >&2
fi

# Honor the escape hatch whether exported or written inline in the command
# (the checker itself also honors the exported form).
if printf '%s' "$payload" | grep -q 'GUMNUT_SKIP_PUSH_CHECKS=1'; then
  echo "pre-push-checks: skipped (GUMNUT_SKIP_PUSH_CHECKS=1)" >&2
  exit 0
fi

script_dir=$(cd "$(dirname "$0")" && pwd)
if "$script_dir/../../scripts/pre-push-checks.sh"; then
  exit 0
else
  exit 2
fi
