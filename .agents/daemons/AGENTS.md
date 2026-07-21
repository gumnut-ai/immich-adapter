# Daemons

This directory holds **daemons** — repo-defined operating roles that give recurring
operational debt an explicit owner instead of handling it ad hoc. Each daemon runs
bounded maintenance work triggered by events or a schedule.

Each daemon is one subdirectory with a single `DAEMON.md`:

```
.agents/daemons/<daemon-id>/DAEMON.md
```

The format of `DAEMON.md` — frontmatter fields, activation, and body conventions — is
defined by the spec. Follow it as the source of truth rather than relying on the
fields used by daemons already in this directory, since the spec may change over time:

**https://docs.charlielabs.ai/daemons**

## Repo conventions

- Keep `<daemon-id>` and the frontmatter `id` in sync with each other.
- Keep each daemon to a single, well-bounded role. Split unrelated concerns into
  separate daemons rather than overloading one.
- Prefer the smallest safe change, and state explicit limits (open-PR caps,
  commits-per-activation, one concern per PR) so activations stay bounded.

## Current daemons

- `codebase-maintainer/` — keeps dependencies current and the codebase clean.
- `librarian/` — keeps this repo's documentation current and complete.
- `pr-simplicity-review/` — advises PR authors on removable implementation complexity.
