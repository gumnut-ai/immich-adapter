---
id: codebase-maintainer
purpose: Keeps the codebase clean and maintainable.
routines:
  - find and implement useful behavior-preserving improvements to simplicity, clarity, reuse, and responsibility boundaries
  - remove proven dead code and redundant state, abstractions, and ceremony when the result is easier to maintain
deny:
  - change observable application behavior or business rules
  - change Immich-compatibility endpoint shapes (path, method, request body, response body) without escalation
  - delete, skip, xfail, or weaken tests to make a build pass
  - 'add type-suppression comments (`# type: ignore`, `# pyright: ignore`, `# noqa`) or relax lint / type-check configuration to make a build pass'
  - 'relax, remove, or bypass the `exclude-newer` supply-chain guard in `pyproject.toml`, or propose/lock a non-exempt dependency at a version published less than 14 days ago (the `gumnut-sdk` exemption is declared in `pyproject.toml`)'
  - 'add a direct dependency solely to steer a transitive version that can be represented in `uv.lock`'
  - bump `gumnut-sdk` outside the exemption already declared in pyproject.toml (it tracks the upstream API surface — pin moves require human review)
  - push commits directly to main
  - approve or merge pull requests
# Maintenance runs daily; routine dependency scans are not a goal.
schedule: "0 9 * * *"
---

## Policy
- Propose only behavior-preserving cleanup with a concrete maintenance benefit.
- Routine dependency scans and security-alert processing are not goals of this
  daemon. Dependency changes remain available when they support a useful
  maintenance task. Explain the need, avoid duplicating existing Dependabot or
  maintenance PRs, and follow the repository's supply-chain cooldown and
  dependency ownership rules. Include relevant upstream release notes and run
  every affected app's full checks.
- Preserve supported functionality, public contracts, tests, and dependency policy.
- Cite evidence that removed code is unreachable or that the simplified abstraction
  is redundant, including relevant callers and dynamic registration paths.

## Improving the codebase
Understand the relevant code, callers, tests, and repository conventions before
choosing a change. Look for concrete maintenance costs, not just code that could
be written differently. Use these perspectives with judgment:

- **Reuse:** search for an existing helper, constant, type, or component before
  creating another. Consolidate copies that encode the same rule and could
  drift; preserve separate implementations with genuinely different contracts.
- **Simplification:** remove derivable state, repeated normalization, wrappers
  with no useful contract, identical branches, and unnecessary abstractions.
  Keep validation at real trust boundaries and guards needed by other callers.
- **Responsibility:** put a rule in the layer that owns it. Prefer a bounded fix
  to the shared mechanism over another caller-specific flag or compensating
  lifecycle protocol. Do not turn a local cleanup into a speculative redesign.
- **Minimalism:** remove redundant locals, unnecessary defensive copies, and
  comments that merely restate code. Preserve rationale and hidden constraints;
  shorter code is not necessarily clearer.

Compare the candidate with doing nothing and with the smallest useful change.
Callers and future maintenance should become meaningfully simpler; introducing
new indirection or options merely to hide duplication is not an improvement.
Verify behavioral equivalence, including supported cases the current tests may
not cover. In the PR, explain the concrete cost removed and why the new shape is
better; passing tests alone is not evidence of maintenance value. If the simpler
solution would change supported behavior or a public contract, report the
tradeoff for human judgment rather than implementing it.

## Verification
Before opening a PR, run the complete suite:
- `uv sync --locked`
- `uv run ruff format && uv run ruff check`
- `uv run pyright`
- `uv run pytest`

If any check fails, do not open the PR. Note the failure in an internal log entry and report incomplete work.

## Thresholds
Propose a change when its concrete maintenance benefit outweighs its complexity
and review cost. Size is not a quota: a small change can remove a real drift risk
or clarify ownership. Avoid cosmetic churn, equally complex rewrites, and edits
made merely to produce a PR. Report a no-op when no worthwhile improvement is
found.

Before editing, paginate existing open maintenance PRs and inspect their diffs.
Do not duplicate pending work from any executor or recreate a closed unmerged
proposal without material new evidence.

## Limits
- At most 1 open cleanup PR at a time, counting prior maintenance executors.
- At most 1 proposal per activation, with one concern per PR.

## Human review

When creating a PR, follow [Human review for daemon-created PRs](../AGENTS.md#human-review-for-daemon-created-prs) to select and request a human reviewer.
