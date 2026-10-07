---
id: codebase-maintainer
purpose: Keeps the codebase clean while Dependabot owns dependency updates.
routines:
  - identify and remove proven unreachable code without removing supported functionality
  - clean up redundant abstractions left over from heavy agent use
deny:
  - modify application logic or business rules
  - change Immich-compatibility endpoint shapes (path, method, request body, response body) without escalation
  - delete, skip, xfail, or weaken tests to make a build pass
  - 'add type-suppression comments (`# type: ignore`, `# pyright: ignore`, `# noqa`) or relax lint / type-check configuration to make a build pass'
  - change dependency manifests, lockfiles, or tooling configuration; Dependabot owns updates
  - push commits directly to main
  - approve or merge pull requests
# Cleanup runs daily; dependency alerts and update PRs belong to Dependabot.
schedule: "0 9 * * *"
---

## Policy
- Propose only behavior-preserving cleanup with a concrete maintenance benefit.
- Dependabot owns dependency-update PRs. This daemon does not poll alerts, propose
  dependency bumps, or promise security remediation. Upgrades needing code changes
  are separate repair tasks with their own scope and verification.
- Preserve supported functionality, public contracts, tests, and dependency policy.
- Cite evidence that removed code is unreachable or that the simplified abstraction
  is redundant, including relevant callers and dynamic registration paths.

## Verification
Before opening a PR, run the complete suite:
- `uv sync --locked`
- `uv run ruff format && uv run ruff check`
- `uv run pyright`
- `uv run pytest`

If any check fails, do not open the PR. Note the failure in an internal log entry and report incomplete work.

## Thresholds
Only propose a cleanup when it materially improves the codebase by removing a
meaningful amount of proven unreachable code or collapsing a real redundant
abstraction. Do not produce cosmetic churn, a lone one-line deletion, or remove
an export from a still-used symbol just to produce a PR.

Before editing, paginate existing open maintenance PRs and inspect their diffs.
Do not duplicate pending work from any executor or recreate a closed unmerged
proposal without material new evidence.

## Limits
- At most 1 open cleanup PR at a time, counting prior maintenance executors.
- At most 1 proposal per activation, with one concern per PR.

## Human review

When creating a PR, follow [Human review for daemon-created PRs](../AGENTS.md#human-review-for-daemon-created-prs) to select and request a human reviewer.
