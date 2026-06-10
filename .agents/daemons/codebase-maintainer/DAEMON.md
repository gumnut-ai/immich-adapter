---
id: codebase-maintainer
purpose: Keeps the codebase clean, secure, and current.
watch:
  - when a pull request is merged into main
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
- Open a security-patch PR within 24 hours of an advisory affecting this repo.

## Limits
- At most 3 open dependency PRs from this daemon at a time.
- At most 1 open dead-code cleanup PR at a time.
- One concern per PR — never bundle a dep bump with a cleanup.
