# Daemons

This directory holds **daemons** — repo-defined operating roles that give recurring
operational debt an explicit owner instead of handling it ad hoc. Each daemon runs
bounded maintenance work triggered by events or a schedule.

Each daemon is one subdirectory with a `DAEMON.md`, plus any reference files it reads:

```
.agents/daemons/<daemon-id>/DAEMON.md
.agents/daemons/<daemon-id>/references/...
```

These files are retained repository-owned role policies for proposed or
individually gated native workflows. They do not establish active coverage. Their frontmatter records the role's purpose and limits; the owning
workflow defines executable activation and scheduling. A policy file alone does
not enable a role. Verify activation through the workflow's documented gates.

Humans review and merge PRs. The existing Codex integration provides automated
review when requested with `@codex review`; no native replacement reviewer is
active.

Review lanes live under `pr-review/references/lanes/`. Each inline finding carries
a `` `§ <lane-name>` `` badge taken from the lane's filename.

## Repo conventions

- Follow [Agent Workflow Design](../../docs/references/agent-workflows.md) when
  authoring or reviewing daemon policies and their supporting scripts.
- Keep `<daemon-id>` and the frontmatter `id` in sync with each other.
- Verify policy and lane changes through the owning workflow's contract checks
  and review the resulting guidance. Maintain the role list below in the same change.
- Keep each daemon to a single, well-bounded role. Split unrelated concerns into
  separate daemons rather than overloading one. Several review dimensions of one
  role are not separate concerns — express them as lanes of that daemon (below)
  rather than as sibling daemons that each activate on the same event.
- A daemon whose role has distinct review dimensions keeps each in its own file
  under `<daemon-id>/references/lanes/`. Every lane requires `## Use this lane when`
  (the use/skip conditions that gate it) and `## What this lane should catch` (each
  finding as **Report when** / **Evidence** / optional **Do not report**); further
  lane-specific sections are fine. Give every lane a skip condition — a lane that
  runs on every activation gates nothing. Keep lane-specific calibration in the
  lane and shared review policy in `DAEMON.md`; where a repository convention
  owns the policy, have the daemon cite this repo's copy of it and derive the
  mechanics at run time instead of restating them — a daemon that restates policy
  it does not own is where the two drift out of agreement. Apply this when adding
  a daemon or substantively editing one; existing restatements are brought under
  it in the edit that next touches them, not in a sweep.
- Prefer the smallest safe change, and state explicit limits (open-PR caps,
  commits-per-activation, one concern per PR) so activations stay bounded.
- A shell command in a `DAEMON.md` is the daemon's implementation, not an
  illustration — run it against this repo exactly as written and check its
  output before committing. Instrumenting a copy with an extra `echo` tests the
  copy, not the block; needing one to see the result means the block isn't
  reporting it. Valid flags are not enough either: a well-formed command can
  silently answer a different question (`git log -G'pydantic'` is an unanchored
  regex, so it reports the date `pydantic-settings` changed).

## Existing PRs before new work

For native PR-producing daemons, check the role's open `gumnut-bot[bot]` PRs
before new work. Prioritize verified review feedback from trusted collaborators
and configured review bots within existing tools, permissions, scope and limits.
Record feedback outside those limits as informational; it does not block the
activation. Follow existing discovery, caps and outcome rules; don't replace PRs.

## Human review for daemon-created PRs

For a daemon-created pull request, select one eligible human GitHub reviewer
and request review on the later readiness activation described below. A PR assignee or a mention in its body is not a review request.
Preserve existing review requests; an existing eligible human reviewer satisfies
this requirement.

Choose the reviewer using recent, substantive contributions to the changed
files, weighing both frequency and recency. Use GitHub commit or merged-PR
history so a shallow checkout does not distort the evidence. For new files or
sparse history, widen to the owning directory or related area, then an active
repository maintainer. Resolve the GitHub account from that evidence rather
than guessing a login from a commit name or email. Exclude the PR author, bots,
and known automation accounts; confirm the candidate can review this repository.

Before new discovery or applying creation caps, inspect existing same-repository
PRs managed by the role and targeting main for missing human review requests. Creation caps do not
skip this readiness inspection. Request review only after the exact current head
has passed required checks, has no known relevant failures or unresolved
actionable findings, and is clean and mergeable with main. Never hand off code
changed in this activation; new proposals wait for CI and a later activation.
Use native `add_reviewer` with numeric `pull_request_number`. Preserve existing
humans; an eligible pending request or completed review on the current head
satisfies review delivery. Assignment alone does not satisfy that requirement.
When a role requires assignment, use native `assign_to_user` with numeric
`issue_number` only if an eligible assignment is missing. If both assignment
and review delivery are missing, emit one call for each; if both are satisfied,
emit neither. Assignment is required only where the role policy calls for it.
Emit only missing actions, at most five readiness-only handoffs per activation.
Inspect actual publication results; failed or unconfirmed actions remain
incomplete handoffs. Never approve, merge, delete branches or operate production.

## Role policies

- `codebase-maintainer/` — proposes useful codebase maintenance without routine dependency scans.
- `dependency-maintenance/` — repairs dependency PRs and proposes useful compatible updates daily.
- `librarian/` — keeps this repo's documentation current and complete.
- `pr-review/` — reviews pull requests under repo-authored policy, across the lanes
  in `pr-review/references/lanes/`.
