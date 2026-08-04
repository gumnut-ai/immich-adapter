# Daemons

This directory holds **daemons** — repo-defined operating roles that give recurring
operational debt an explicit owner instead of handling it ad hoc. Each daemon runs
bounded maintenance work triggered by events or a schedule.

Each daemon is one subdirectory with a `DAEMON.md`, plus any reference files it reads:

```
.agents/daemons/<daemon-id>/DAEMON.md
.agents/daemons/<daemon-id>/references/...
```

The format of `DAEMON.md` — frontmatter fields, activation, and body conventions — is
defined by the spec. Follow it as the source of truth rather than relying on the
fields used by daemons already in this directory, since the spec may change over time:

**https://docs.charlielabs.ai/daemons**

The spec covers `DAEMON.md` itself. It documents `references/` only as material a daemon
may read, so the `references/lanes/` layout below is this repo's convention and this file
is its only source of truth. To confirm lanes still load the way it assumes, read a recent
`pr-review` review on GitHub: each inline finding carries a `` `§ <lane-name>` `` badge
taken from the lane's filename.

## Repo conventions

- Keep `<daemon-id>` and the frontmatter `id` in sync with each other.
- No CI validates these files — a malformed daemon, or a lane at the wrong path, fails
  silently by producing no findings rather than an error. Verify changes by hand, and
  maintain the daemon list below yourself: a bot PR that installs a daemon adds its
  directory without touching this file.
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
  lane and shared review policy in `DAEMON.md`: a lane that restates daemon policy
  is where the two drift out of agreement.
- Prefer the smallest safe change, and state explicit limits (open-PR caps,
  commits-per-activation, one concern per PR) so activations stay bounded.
- A shell command in a `DAEMON.md` is the daemon's implementation, not an
  illustration — run it against this repo exactly as written and check its
  output before committing. Instrumenting a copy with an extra `echo` tests the
  copy, not the block; needing one to see the result means the block isn't
  reporting it. Valid flags are not enough either: a well-formed command can
  silently answer a different question (`git log -G'pydantic'` is an unanchored
  regex, so it reports the date `pydantic-settings` changed).

## Current daemons

- `codebase-maintainer/` — keeps dependencies current and the codebase clean.
- `librarian/` — keeps this repo's documentation current and complete.
- `pr-review/` — reviews pull requests under repo-authored policy, across the lanes
  in `pr-review/references/lanes/`.
