# Important

Before starting work, read `README.md` for project setup and consult the Documentation Map below for relevant docs.

**This repo is public, so everything committed here has to stand on its own.** A reader who can't open a Gumnut ticket or a private sibling repo (`gumnut-ai/photos`, `gumnut-ai/gumnut-dev-setup`) still needs the full picture from code, docs, commit messages, and PR bodies. That's not because those things are secret — they aren't — but because a pointer nobody can follow carries no information. So put the context in the repo: write the rationale instead of citing a tracker ID, describe the contract instead of citing a private file path, and call the backend by its public name — **the Gumnut API** (`api.gumnut.ai`) — never the internal `photos-api`. The consolidated convention, with examples, is in `docs/references/project-conventions.md` § Project Conventions.

Concrete violations that have actually shipped: cross-link lines like `Cross-link: gumnut-ai/photos#NNN` in a PR description (the PR number resolves to a 404 for outsiders) and "see `photos-api/services/...`" file-path references. When a Linear issue or design doc that lives in a private repo asks you to "cross-link the photos-api PR," do not copy that framing verbatim — describe what the other repo's change does instead.

Separately — and this one *is* about confidentiality — captured example data in committed docs (sync payloads, request/response logs, packet traces) must use placeholder PII — replace real names, emails, and LAN IPs with `Example User` / `user@example.com` / `192.0.2.x`, keeping only the technical fields the example actually teaches (UUIDs, checksums, timestamps, wire shapes). Real personal data in a public repo is exposure regardless of how it got there; pruning/restoring such a doc is the moment to redact, not to faithfully preserve the capture.

# Pre-Commit Commands

Run from the `immich-adapter/` directory:

- **Format**: `uv run ruff format`
- **Lint**: `uv run ruff check`
- **Type check**: `uv run pyright`
- **Test**: `uv run pytest`
- **Docs**: `uv run scripts/lint_docs.py` (`--fix` bumps stale `last-updated:` dates) — see § Documentation Checks below

# Documentation Checks

`scripts/lint_docs.py` runs on every PR in `.github/workflows/ci.yml`; use `uv run scripts/lint_docs.py --fix` to bump missed dates. The complete frontmatter, map, lifecycle, path, and review contract is local in `docs/references/documentation-conventions.md`. The linter is kept byte-identical to the Gumnut Photos copy, with repo differences in `scripts/lint_docs.toml`.

# Documentation Map

This file is a concise quick-reference. Detailed content belongs in the appropriate `docs/` subdirectory, not here. Add new topics to the table below and create a corresponding doc file.

Detailed docs live in subdirectories: `docs/architecture/` (system architecture), `docs/references/` (coding patterns, conventions), `docs/guides/` (setup and workflow guides), `docs/design-docs/` (design decisions with status frontmatter). Consult these when working in the relevant areas.

The sections below follow that order, with design docs split by their `status:` frontmatter: `proposed` or `active` under Active Design Docs, `completed` or `deprecated` under Historical & Deprecated Design Docs. Reclassifying a design doc moves its row — a retired doc still listed as active sends readers to a frozen answer.

## Architecture

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Adapter architecture | `docs/architecture/adapter-architecture.md` | Adapter boundary, request and data translation, persistence/custody routing, timeline stack collapse, trash, sync/realtime routing, and failure behavior |
| Sync stream architecture | `docs/architecture/sync-stream-architecture.md` | Sync stream event processing, FK ordering, event classification, face/album handling, adding new sync type versions |
| WebSocket implementation | `docs/architecture/websocket-implementation.md` | WebSocket connections, real-time sync, event handling |
| Session & checkpoint implementation | `docs/architecture/session-checkpoint-implementation.md` | Session management, checkpoint tracking, sync state |

## References

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Project conventions | `docs/references/project-conventions.md` | Python style, repository organization, public-repository wording, and pull requests |
| Routes and compatibility | `docs/references/routes-dtos-and-upstream-compatibility.md` | Route parameters, DTOs, errors, generated models, upstream behavior, version bumps, and the checklist for promoting a stub to a real implementation |
| Asset and media handling | `docs/references/asset-and-media-handling.md` | Asset fields, media variants, checksums, face geometry, and asset-operation WebSocket emission |
| Pagination, bulk, and concurrency | `docs/references/pagination-bulk-and-concurrency.md` | Cursor/offset translation, bounded enumeration, aggregates, fan-out, and bulk-ID operations |
| Testing and logging | `docs/references/testing-and-logging.md` | Test fixtures, async test traps, structured logging, and upstream severity policy |
| Documentation conventions | `docs/references/documentation-conventions.md` | Writing or maintaining docs — frontmatter, map rows, lifecycle, freshness, path citations |
| GitHub Actions best practices | `docs/references/github-actions-best-practices.md` | Writing or reviewing workflows — action pins, permissions, untrusted triggers, shell interpolation, zizmor |
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
| Large upload timeout | `docs/design-docs/large-upload-timeout.md` | Streaming upload pipeline, large file upload failures, Immich client timeout limits |
| Immich adapter gap analysis | `docs/design-docs/immich-adapter-gap-analysis.md` | Prioritizing adapter work, evaluating stub endpoints, assessing feature gaps |

## Historical & Deprecated Design Docs

Decision records, not descriptions of the running system — consult them for *why* something was chosen, never for how it works today.

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Authentication design | `docs/design-docs/auth-design.md` | Why session tokens replaced raw JWTs, and the constraints that forced it — current auth is in `docs/architecture/adapter-architecture.md` |
| Render deploy with Docker | `docs/design-docs/render-deploy-docker.md` | Why the adapter deploys as a multi-stage Docker image, and the pinned-vs-floating base-image trade-off |
| Checksum support | `docs/design-docs/checksum-support.md` | Why a dedicated SHA-1 column beat a side table — current checksum rules are in `docs/references/asset-and-media-handling.md` |
| Trash soft-delete (adapter) | `docs/design-docs/trash-soft-delete-adapter.md` | The original adapter-side trash design record — current trash/restore semantics are in `docs/architecture/adapter-architecture.md` |
| Static file sharing | `docs/design-docs/static-file-sharing.md` | Why the adapter serves Immich static files itself and chose Docker extraction — current operation is in the web guide |
| Sync stream event ordering | `docs/design-docs/sync-stream-event-ordering.md` | Why event replay required parent-first upserts, child-first deletes, and current-state FK verification |
| Immich v3 API change analysis | `docs/design-docs/immich-v3-api-changes.md` | Why the adapter made a clean Immich v3 retarget and how behavioral changes were separated from codegen noise |
