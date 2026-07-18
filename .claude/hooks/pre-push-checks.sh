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
    # is embedded in a single-quoted shell string.) Deliberate carve-outs:
    #   - Backslash escapes are honored: outside quotes, an escaped quote
    #     does not open a span; inside double quotes it does not close one.
    #   - A double-quoted span containing $( or ` re-enters command context
    #     (command substitution really pushes) — left unmasked.
    #   - If a quote never terminates, return the RAW string and err broad:
    #     masking away a real push would fail open.
    out = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            out.append(s[i:i + 2])
            i += 2
        elif ch == "\x27":
            j = i + 1
            while j < n and s[j] != "\x27":
                j += 1
            if j >= n:
                return s
            out.append(ch + " " * (j - i - 1) + ch)
            i = j + 1
        elif ch == "\"":
            j = i + 1
            cmdsub = False
            while j < n and s[j] != "\"":
                if s[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if s[j] == "`" or (s[j] == "$" and j + 1 < n and s[j + 1] == "("):
                    cmdsub = True
                j += 1
            if j >= n:
                return s
            if cmdsub:
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

# `git` must sit in command position; `push` must be a standalone token. A
# QUOTED subcommand (`git "push"`) still executes a push after shell quote
# removal, so a second pass over the ORIGINAL text catches it. Residual
# over-match: an unquoted free-text `push` argument to a real git command
# still matches — err broad; the fix is quoting the message.
CMD_POS = r"(?:^|[;&|\n(])\s*(?:(?:env|command|exec)\s+)*(?:[A-Za-z_][A-Za-z_0-9]*=(?:[^\s;|&]|\"[^\"]*\"|\x27[^\x27]*\x27)*\s+)*"
# Optional path prefix: `/usr/bin/git push` is still a push.
GIT_TOKEN = r"(?:[^\s;|&]*/)?git(?![\w./-])"
PUSH_RE = CMD_POS + GIT_TOKEN + r"[^|;&\n]*?(?<![\w./-])push(?![\w./-])"
QPUSH_RE = CMD_POS + GIT_TOKEN + r"[^|;&\n]*?[\"\x27]push[\"\x27]"

pushes = list(re.finditer(PUSH_RE, masked))
starts = set(p.start() for p in pushes)
pushes += [m for m in re.finditer(QPUSH_RE, cmd) if m.start() not in starts]
pushes.sort(key=lambda m: m.start())
if not pushes:
    sys.exit(NOT_A_PUSH)

# Shell scope model for exports: chains computed at match END (the match
# START is the anchor, which may itself be the paren opening the scope).
def stack_at(pos):
    st = []
    for k in range(pos):
        c = masked[k]
        if c == "(":
            st.append(k)
        elif c == ")" and st:
            st.pop()
    return st

def applies(x, p):
    if x.start() >= p.start():
        return False
    xs, ps = stack_at(x.end()), stack_at(p.end())
    return xs == ps[:len(xs)]

# Skip semantics, per push (a quoted mention never counts — masked): the
# segment of the push itself is prefixed with the assignment, or an
# `export GUMNUT_SKIP_PUSH_CHECKS=1` precedes it in an applicable scope.
# A bare assignment on another command does not persist and skips nothing;
# a partial skip leaves the other pushes checked.
exports = list(re.finditer(r"(?:^|[;&|\n(])\s*export\s+GUMNUT_SKIP_PUSH_CHECKS=1(?!\w)", masked))

def is_skipped(p):
    seg = masked[p.start():p.end()]
    git_at = re.search(GIT_TOKEN, seg)
    if git_at and "GUMNUT_SKIP_PUSH_CHECKS=1" in seg[:git_at.start()]:
        return True
    return any(applies(e, p) for e in exports)

if all(is_skipped(p) for p in pushes):
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
