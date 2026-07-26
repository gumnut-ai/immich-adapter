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
- Do not bump the same dependency more than once every 30 days. Security-advisory patches are exempt and are never blocked by this cap — they keep the 24-hour threshold below. Fast-moving dependencies clear the two-minor-versions bar again within days, so without this cap a daily activation re-proposes the same package every week or two, churning CI and review attention for an upgrade that just landed. Before proposing a non-security bump, run both checks below and skip the dependency if either shows it was bumped in the last 30 days:
  - **Merged history** — compare the package's locked version against its version 30 days ago:

    ```bash
    base=$(git rev-list -1 --before='30 days ago' HEAD)
    locked() { grep -A1 "^name = \"$1\"\$" | sed -n 's/^version = "\(.*\)"/\1/p'; }
    now=$(locked <pkg> < uv.lock)
    was=$(git show "${base}:uv.lock" | locked <pkg>)
    ```

    They differ (or `was` is empty, meaning the package was added inside the window) → bumped recently, skip it. Read `uv.lock`, not `pyproject.toml`: most bumps here are re-locks of transitive dependencies that `pyproject.toml` never declares, so a manifest-only check reports "never bumped" for exactly the packages that churn most. Compare versions rather than searching for commits that touched the package's lines: `uv lock` rewrites artifact metadata without changing any version (commit `93d0241` only appended `upload-time=`), so a commit-based check reads that as a bump for every package at once and suppresses the whole dependency set for a month. Reading the `name`/`version` fields also sidesteps PyPI's filename normalization — `<pkg>` substitutes literally. Keep the braces in `"${base}:uv.lock"` — in zsh, `"$base:uv.lock"` parses `:u` as an upcase modifier and silently reads the wrong path. Empty `base` means the checkout's history doesn't reach back 30 days (e.g. a shallow clone); run `git fetch --unshallow` before trusting the comparison.
  - **Open PRs** — this daemon's own unmerged bump PRs, which merged history cannot see: `gh pr list --state open --author 'app/charliecreates' --limit 100` (the same set counted against the 3-PR limit in Limits). Confirm against each candidate's `uv.lock` hunks (`gh pr diff <n>`) rather than trusting the title. A dependency bumped in one of them counts as bumped today — don't re-propose it.

## Limits
- At most 3 open dependency PRs from this daemon at a time.
- At most 1 open dead-code cleanup PR at a time.
- One concern per PR — never bundle a dep bump with a cleanup.
