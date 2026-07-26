---
id: codebase-maintainer
purpose: Keeps the codebase clean, secure, and current.
watch:
  - when a security advisory is published for a dependency in this repo
routines:
  - propose tested upgrade PRs for outdated dependencies
  - propose tested PRs that patch known security vulnerabilities
  - identify and remove dead code, unused endpoints, routers, or services
  - clean up redundant abstractions left over from heavy agent use
deny:
  - modify application logic or business rules
  - change Immich-compatibility endpoint shapes (path, method, request body, response body) without escalation
  - delete, skip, xfail, or weaken tests to make a build pass
  - 'add type-suppression comments (`# type: ignore`, `# pyright: ignore`, `# noqa`) or relax lint / type-check configuration to make a build pass'
  - 'relax, remove, or bypass the `exclude-newer` supply-chain guard in `pyproject.toml`, or propose/lock a non-exempt dependency at a version published less than 14 days ago (the `gumnut-sdk` exemption is declared in `pyproject.toml`)'
  - bump `gumnut-sdk` outside the exemption already declared in pyproject.toml (it tracks the upstream API surface — pin moves require human review)
  - push commits directly to main
  - approve or merge pull requests
# Daily, not every 6h: dependency upgrades (gated on "2+ minor versions behind"
# and the 14-day cooldown) and dead-code cleanup are low-urgency maintenance, so
# four scheduled passes a day mostly re-evaluate unchanged state and churn CI on
# the open PRs. Security advisories are NOT gated by this cron — they fire on the
# `when a security advisory is published` watch above (24h SLA), so the slower
# cadence doesn't slow the urgent path.
schedule: "0 9 * * *"
---

## Policy
- Prefer the smallest safe change. A dependency bump, not a rewrite.
- Every upgrade PR must include passing tests.
- Every dependency upgrade PR description must summarize what changed in the dependency between the old and new version — pull from the dependency's release notes / changelog (e.g. GitHub Releases, `CHANGELOG.md`) for the bumped range. Call out behavior changes, deprecations, and breaking changes relevant to how this repo uses the dependency, and link the upstream changelog/release. If no changelog is available, say so and link the version-diff (e.g. the compare view between the two tags) instead.
- Respect the `exclude-newer = "14 days"` supply-chain guard in `pyproject.toml` — only consider package versions that satisfy it.

## Verification
Resolve each dependency bump by re-locking with `uv lock` (it honors `exclude-newer`) — never hand-edit `uv.lock`. Then, before opening a PR, run:
- `uv sync --locked`
- `uv run ruff format && uv run ruff check`
- `uv run pyright`
- `uv run pytest`

If any check fails, do not open the PR. Note the failure in an internal log entry and leave the upgrade pending.

## Thresholds
- Upgrade a dependency only when it is at least two minor versions behind the latest stable that the supply-chain guard allows.
- Do not bump the same dependency more than once every 30 days. Security-advisory patches are exempt — they keep the 24-hour threshold below. Fast-moving dependencies clear the two-minor-versions bar again within days, so without this cap a daily activation re-proposes the same package every week or two, churning CI and review attention for an upgrade that just landed. Before proposing a non-security bump, run both checks below and skip the dependency if either shows it was bumped in the last 30 days:
  - **Merged history** — compare the package's locked version against its version 30 days ago. Run the block as a script with the package name as its argument:

    ```bash
    pkg=${1:?usage: check-bump-cadence <package-name>}
    base=$(git rev-list -1 --before='30 days ago' HEAD)
    [ -n "$base" ] || { echo "no commit older than 30 days — unshallow the clone, or skip this check if the repo itself is younger"; exit 1; }
    oldlock=$(git show "${base}:uv.lock") || { echo "no uv.lock at ${base}"; exit 1; }
    locked() { grep -A1 "^name = \"$1\"\$" | sed -n 's/^version = "\(.*\)"/\1/p'; }
    now=$(locked "$pkg" < uv.lock)
    was=$(printf '%s\n' "$oldlock" | locked "$pkg")
    [ -n "$now" ] || { echo "$pkg not in uv.lock — pass the name exactly as the lockfile spells it"; exit 1; }
    if [ "$now" = "$was" ]; then
      echo "$pkg unchanged at $now since ${base} — eligible"
    else
      echo "$pkg ${was:-absent} -> $now — bumped within 30 days, skip"; exit 2
    fi
    ```

    Exit 2 means skip, 0 means eligible, 1 means the check could not tell — which is what the three guards buy. Each one covers a path that otherwise returns a plausible answer instead of failing: an empty `base` makes `git show ":uv.lock"` read the *index* (valid syntax), so every package compares equal and the cap passes everything while looking like it ran; and a name absent from `uv.lock` — a non-canonical spelling such as `pydantic_settings` — leaves both versions empty, which also compares equal. Read `uv.lock`, not `pyproject.toml`: most bumps here are re-locks of transitive dependencies that `pyproject.toml` never declares, so a manifest-only check reports "never bumped" for exactly the packages that churn most. Compare versions rather than searching for commits that touched the package's lines: a `uv lock` run rewrites artifact metadata across the whole file without changing versions (commit `93d0241` appended `upload-time=` to every package's `sdist`/`wheels` lines while changing exactly one real version), so a commit-based check reads that as a bump for every package at once and suppresses the whole dependency set for a month. Reading the `name`/`version` fields keeps the lookup on canonical names rather than PyPI's normalized filenames (`ua_parser-`), so pass the name exactly as `uv.lock` spells it. Keep the braces in `"${base}:uv.lock"` — in zsh, `"$base:uv.lock"` parses `:u` as an upcase modifier and silently reads the wrong path.
  - **Open PRs** — this daemon's own unmerged bump PRs, which merged history cannot see: `gh pr list --state open --author 'app/charliecreates' --limit 100`. That author also covers the `librarian` daemon's `docs:` PRs and this daemon's own cleanup PRs, so count only the ones whose diff touches `uv.lock` against the 3-PR dependency limit in Limits. Confirm against each candidate's `uv.lock` hunks (`gh pr diff <n>`) rather than trusting the title. A dependency bumped in one of them counts as bumped today — don't re-propose it.
- Open a security-patch PR within 24 hours of an advisory affecting this repo.

## Limits
- At most 3 open dependency PRs from this daemon at a time.
- At most 1 open dead-code cleanup PR at a time.
- One concern per PR — never bundle a dep bump with a cleanup.
