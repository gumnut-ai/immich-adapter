# Important

Before starting work, read `README.md` for project setup and consult the Documentation Map below for relevant docs.

**This repo is public, so everything committed here has to stand on its own.** A reader who can't open a Gumnut ticket or a private sibling repo (`gumnut-ai/photos`, `gumnut-ai/gumnut-dev-setup`) still needs the full picture from code, docs, commit messages, and PR bodies. That's not because those things are secret — they aren't — but because a pointer nobody can follow carries no information. So put the context in the repo: write the rationale instead of citing a tracker ID, describe the contract instead of citing a private file path, and call the backend by its public name — **the Gumnut API** (`api.gumnut.ai`) — never the internal `photos-api`. The consolidated convention, with examples, is in `docs/references/code-practices.md` § Project Conventions.

Concrete violations that have actually shipped: cross-link lines like `Cross-link: gumnut-ai/photos#NNN` in a PR description (the PR number resolves to a 404 for outsiders) and "see `photos-api/services/...`" file-path references. When a Linear issue or design doc that lives in a private repo asks you to "cross-link the photos-api PR," do not copy that framing verbatim — describe what the other repo's change does instead.

Separately — and this one *is* about confidentiality — captured example data in committed docs (sync payloads, request/response logs, packet traces) must use placeholder PII — replace real names, emails, and LAN IPs with `Example User` / `user@example.com` / `192.0.2.x`, keeping only the technical fields the example actually teaches (UUIDs, checksums, timestamps, wire shapes). Real personal data in a public repo is exposure regardless of how it got there; pruning/restoring such a doc is the moment to redact, not to faithfully preserve the capture.

# Pre-Commit Commands

Run from the `immich-adapter/` directory:

- **Format**: `uv run ruff format`
- **Lint**: `uv run ruff check`
- **Type check**: `uv run pyright`
- **Test**: `uv run pytest`
- **Docs**: `python3 scripts/lint_docs.py` (`--fix` bumps stale `last-updated:` dates) — see § Documentation Checks below

# Documentation Checks

`scripts/lint_docs.py` enforces the mechanically checkable half of the conventions below, and runs on every PR (the `lint-docs` job in `.github/workflows/ci.yml`). It is stdlib-only, so it needs no install step and runs anywhere `python3` does.

| Check | Enforces |
|-------|----------|
| `freshness` | `last-updated:` bumped on an edited doc, and current on a doc added on the branch |
| `links` | Every inline markdown link target resolves |
| `anchors` | Every `#fragment` resolves to a real heading or `<a id>` |
| `map_paths` | Every Documentation Map row's path resolves, as does every `superseded-by:`; warns on a design doc with no row |
| `map_status_section` | A design doc's map section matches its `status:` frontmatter |
| `map_cells` | The Consult-when cell stays one line, under 250 characters |
| `frontmatter` | `docs/design-docs/` needs `title`/`status`/`created`/`last-updated`, plus `superseded-by` when deprecated; other `docs/` need `title`/`last-updated` |

Only `freshness` is diff-scoped. The rest run repo-wide, deliberately: renaming a doc breaks inbound links and map rows in files the change never touched. Checks are individually switchable in `scripts/lint_docs.toml`, and `--check <name>` overrides that so a disabled rule can be swept before being switched on.

The file is kept byte-identical to the copy in the Gumnut Photos repo — repo differences belong in `lint_docs.toml`, not the script. A green run is not a full review: the linter buys reachability and structural consistency, never correctness.

# Documentation Map

This file is a concise quick-reference. Detailed content belongs in the appropriate `docs/` subdirectory, not here. Add new topics to the table below and create a corresponding doc file.

Detailed docs live in subdirectories: `docs/architecture/` (system architecture), `docs/references/` (coding patterns, conventions), `docs/guides/` (setup and workflow guides), `docs/design-docs/` (design decisions with status frontmatter). Consult these when working in the relevant areas.

The sections below follow that order, with design docs split by their `status:` frontmatter: `proposed` or `active` under Active Design Docs, `completed` or `deprecated` under Historical & Deprecated Design Docs. Reclassifying a design doc moves its row — a retired doc still listed as active sends readers to a frozen answer.

## Architecture

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Adapter architecture | `docs/architecture/adapter-architecture.md` | Overall adapter design, auth and session handling, request observability (Sentry tags and user attribution), trash/restore semantics, data translation, pagination, sync protocol, error handling, endpoint status |
| Sync stream architecture | `docs/architecture/sync-stream-architecture.md` | Sync stream event processing, FK ordering, event classification, face/album handling, adding new sync type versions |
| WebSocket implementation | `docs/architecture/websocket-implementation.md` | WebSocket connections, real-time sync, event handling |
| Session & checkpoint implementation | `docs/architecture/session-checkpoint-implementation.md` | Session management, checkpoint tracking, sync state |

## References

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Code practices | `docs/references/code-practices.md` | Python style, project conventions, endpoint patterns, checksum handling and deduplication, error handling, testing, logging, PR practices |
| WebSocket events reference | `docs/references/websocket-events-reference.md` | WebSocket event types, payload formats |
| Session & checkpoint reference | `docs/references/session-checkpoint-reference.md` | Session/checkpoint object shapes, field definitions |
| Immich sync communication | `docs/references/immich-sync-communication.md` | Immich client-server sync protocol, message formats |
| Uvicorn settings | `docs/references/uvicorn-settings.md` | Server configuration, worker settings, timeouts, the Render `$PORT` binding contract |
| Development tools | `docs/references/development-tools.md` | Model generation, API compatibility, OpenAPI spec, Renovate automation |

## Guides

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Running with Immich Web | `docs/guides/running-with-immich-web.md` | Setting up the full local stack (Immich web + adapter + the Gumnut API + Clerk OAuth) |
| Running with Immich Mobile | `docs/guides/running-with-immich-mobile.md` | Self-signed certs, HTTPS setup, connecting the Immich mobile app |
| Importing with immich-go | `docs/guides/importing-with-immich-go.md` | Bulk-importing a library with the immich-go CLI, `x-api-key` auth via a Gumnut API key |

## Active Design Docs

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Static file sharing | `docs/design-docs/static-file-sharing.md` | File sharing proposals, static asset serving |
| Sync stream event ordering | `docs/design-docs/sync-stream-event-ordering.md` | Sync FK integrity, event ordering, face/person deletion issues |
| Large upload timeout | `docs/design-docs/large-upload-timeout.md` | Streaming upload pipeline, large file upload failures, Immich client timeout limits |
| Immich adapter gap analysis | `docs/design-docs/immich-adapter-gap-analysis.md` | Prioritizing adapter work, evaluating stub endpoints, assessing feature gaps |
| Immich v3 API change analysis | `docs/design-docs/immich-v3-api-changes.md` | Planning an Immich 3.0 retarget, reviewing breaking API diffs, and scoping compatibility work |

## Historical & Deprecated Design Docs

Decision records, not descriptions of the running system — consult them for *why* something was chosen, never for how it works today.

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Authentication design | `docs/design-docs/auth-design.md` | Why session tokens replaced raw JWTs, and the constraints that forced it — current auth is in `docs/architecture/adapter-architecture.md` |
| Render deploy with Docker | `docs/design-docs/render-deploy-docker.md` | Why the adapter deploys as a multi-stage Docker image, and the pinned-vs-floating base-image trade-off |
| Checksum support | `docs/design-docs/checksum-support.md` | Why a dedicated SHA-1 column beat a side table — current checksum rules are in `docs/references/code-practices.md` |
| Trash soft-delete (adapter) | `docs/design-docs/trash-soft-delete-adapter.md` | The original adapter-side trash design record — current trash/restore semantics are in `docs/architecture/adapter-architecture.md` |
