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
# sed/grep its regex semantics don't vary between BSD and GNU tools.
# Matching principles:
#   - Quoted spans are DATA, not command: they are masked (offset-preserving)
#     before any matching, so a commit message mentioning "git push" or the
#     skip flag can neither trigger nor bypass the hook.
#   - `push` must be a standalone token ((?<![\w./-]) / (?![\w./-])), so
#     "pre-push-checks.sh" or "pre-push checks" never match.
#   - `git stash push` is blanked first — a stash is what an agent reaches
#     for when the tree is dirty, i.e. exactly when checks would fail.
#   - The skip flag counts only as a command-position assignment
#     (`GUMNUT_SKIP_PUSH_CHECKS=1 ...`, optionally export/env-prefixed).
# A remaining over-match (a push targeting a *different* repo via
# `git -C ../other-repo push`) still runs this repo's checks — the skip
# assignment is the remedy. Under-matching would let a real push through
# unchecked, so err broad.
#
# python3 exit codes: 0 = push detected; 3 = not a push; 4 = skip flag
# applied; 5 = payload did not parse as JSON (the payload producer is Claude
# Code itself, so malformed means something changed — warn, then fail
# closed). "Not a push" is deliberately NOT exit 1: a broken interpreter
# shim or a syntax error in this embedded program exits 1, and mapping that
# to "not a push" would silently disable the guard forever — any exit
# outside {0,3,4,5} warns and fails closed (runs the checks).
printf '%s' "$payload" | python3 -c '
import json, re, sys

NOT_A_PUSH = 3
SKIPPED = 4
PARSE_FAILURE = 5

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(PARSE_FAILURE)
cmd = payload.get("tool_input", {}).get("command", "")

def mask_quotes(s):
    # Blank quoted contents, keeping quote chars and all offsets intact.
    # ("\x27" is a single quote — spelled as an escape because this program
    # is embedded in a single-quoted shell string.) Two deliberate
    # carve-outs:
    #   - A double-quoted span containing $( or ` re-enters command context
    #     (command substitution: "$(git push)" really pushes) — left
    #     unmasked. Single-quoted spans are pure data in shell.
    #   - If a quote never terminates, this masker cannot trust its read of
    #     the rest (backslash escapes and heredocs it does not model) —
    #     return the RAW string and err broad: over-matching runs checks
    #     needlessly; masking away a real push would fail open.
    out = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in "\"\x27":
            j = i + 1
            while j < n and s[j] != ch:
                j += 1
            if j >= n:
                return s
            content = s[i + 1:j]
            if ch == "\"" and ("$(" in content or "`" in content):
                out.append(s[i:j + 1])
            else:
                out.append(ch + " " * (j - i - 1) + ch)
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)

masked = mask_quotes(cmd)
masked = re.sub(r"(?<![\w./-])stash\s+push(?![\w./-])",
                lambda m: " " * len(m.group(0)), masked)
if not re.search(r"(?<![\w./-])git(?![\w./-])[^|;&\n]*(?<![\w./-])push(?![\w./-])", masked):
    sys.exit(NOT_A_PUSH)
# Any command-position assignment anywhere in the command counts (even one
# not syntactically applied to the push): an unquoted deliberate assignment
# is a clear statement of intent. Quoted mentions never count (masked).
if re.search(r"(?:^|[;&|\n(])\s*(?:export\s+|env\s+)?GUMNUT_SKIP_PUSH_CHECKS=1(?!\w)", masked):
    sys.exit(SKIPPED)
sys.exit(0)
'
rc=$?
if [ "$rc" = "3" ]; then
  exit 0
elif [ "$rc" = "4" ]; then
  echo "pre-push-checks: skipped (GUMNUT_SKIP_PUSH_CHECKS=1)" >&2
  exit 0
elif [ "$rc" = "5" ]; then
  echo "pre-push-checks adapter: hook payload did not parse as JSON — this should not happen (the payload comes from Claude Code itself); investigate. Running checks anyway (fail closed)." >&2
elif [ "$rc" != "0" ]; then
  echo "pre-push-checks adapter: python3 failed (exit $rc); treating command as a push and running checks (fail closed)" >&2
fi

# Checker output goes to stderr (1>&2): on exit 2 the hook contract feeds
# stderr — not stdout — back to the model, and the failure text is the
# actionable part.
script_dir=$(cd "$(dirname "$0")" && pwd)
if "$script_dir/../../scripts/pre-push-checks.sh" 1>&2; then
  exit 0
else
  exit 2
fi
