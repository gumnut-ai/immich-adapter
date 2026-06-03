# Important

Before starting work, read `README.md` for project setup and consult the Documentation Map below for relevant docs.

**This repo is public.** Do not reference private sibling repos (e.g., `gumnut-ai/gumnut-dev-setup`, `gumnut-ai/photos`) from committed files or PR bodies — links resolve to dead ends for external readers and surface the existence of internal docs. Inline the relevant rationale or context instead. Same principle as the existing clarity rule about absolute author-machine paths in committed files.

Concrete violations that have actually shipped: cross-link lines like `Cross-link: gumnut-ai/photos#NNN` in a PR description (the PR number resolves to a 404 for outsiders) and "see `photos-api/services/...`" file-path references. When a Linear issue or design doc that lives in a private repo asks you to "cross-link the photos-api PR," do not copy that framing verbatim — describe what the other repo's change does instead.

Captured example data in committed docs (sync payloads, request/response logs, packet traces) must use placeholder PII — replace real names, emails, and LAN IPs with `Example User` / `user@example.com` / `192.0.2.x`, keeping only the technical fields the example actually teaches (UUIDs, checksums, timestamps, wire shapes). Real personal data in a public repo is exposure regardless of how it got there; pruning/restoring such a doc is the moment to redact, not to faithfully preserve the capture.

# Pre-Commit Commands

Run from the `immich-adapter/` directory:

- **Format**: `uv run ruff format`
- **Lint**: `uv run ruff check`
- **Type check**: `uv run pyright`
- **Test**: `uv run pytest`

# Documentation Map

This file is a concise quick-reference. Detailed content belongs in the appropriate `docs/` subdirectory, not here. Add new topics to the table below and create a corresponding doc file.

Detailed docs live in subdirectories: `docs/architecture/` (system architecture), `docs/design-docs/` (design decisions with status frontmatter), `docs/references/` (coding patterns, conventions), `docs/guides/` (setup and workflow guides). Consult these when working in the relevant areas:

## Architecture

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Adapter architecture | `docs/architecture/adapter-architecture.md` | Overall adapter design, auth, data translation, pagination, sync protocol, error handling, endpoint status |
| Sync stream architecture | `docs/architecture/sync-stream-architecture.md` | Sync stream event processing, FK ordering, event classification, face/album handling, adding new sync type versions |
| WebSocket implementation | `docs/architecture/websocket-implementation.md` | WebSocket connections, real-time sync, event handling |
| Session & checkpoint implementation | `docs/architecture/session-checkpoint-implementation.md` | Session management, checkpoint tracking, sync state |

## Design Docs

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Immich auth architecture | `docs/design-docs/immich-auth-architecture.md` | Legacy auth design (deprecated, see auth-design.md) |
| Authentication design | `docs/design-docs/auth-design.md` | Current auth architecture, OAuth, token handling |
| Static file sharing | `docs/design-docs/static-file-sharing.md` | File sharing proposals, static asset serving |
| Render deploy with Docker | `docs/design-docs/render-deploy-docker.md` | Docker deployment, Render configuration |
| Checksum support | `docs/design-docs/checksum-support.md` | File integrity, checksum validation, deduplication |
| Sync stream event ordering | `docs/design-docs/sync-stream-event-ordering.md` | Sync FK integrity, event ordering, face/person deletion issues |
| Large upload timeout | `docs/design-docs/large-upload-timeout.md` | Streaming upload pipeline, large file upload failures, Immich client timeout limits |
| Immich adapter gap analysis | `docs/design-docs/immich-adapter-gap-analysis.md` | Prioritizing adapter work, evaluating stub endpoints, assessing feature gaps |
| Trash soft-delete (adapter) | `docs/design-docs/trash-soft-delete-adapter.md` | Adapter-side trash/restore/empty semantics, delete `force` mapping, `deletedAt` plumbing, WebSocket trash/restore events |

## Guides

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Running with Immich Web | `docs/guides/running-with-immich-web.md` | Setting up the full local stack (Immich web + adapter + photos-api + Clerk OAuth) |
| Running with Immich Mobile | `docs/guides/running-with-immich-mobile.md` | Self-signed certs, HTTPS setup, connecting the Immich mobile app |

## References

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Code practices | `docs/references/code-practices.md` | Python style, project conventions, endpoint patterns, error handling, testing, logging, PR practices |
| WebSocket events reference | `docs/references/websocket-events-reference.md` | WebSocket event types, payload formats |
| Session & checkpoint reference | `docs/references/session-checkpoint-reference.md` | Session/checkpoint object shapes, field definitions |
| Immich sync communication | `docs/references/immich-sync-communication.md` | Immich client-server sync protocol, message formats |
| Uvicorn settings | `docs/references/uvicorn-settings.md` | Server configuration, worker settings, timeouts |
| Development tools | `docs/references/development-tools.md` | Model generation, API compatibility, OpenAPI spec, Renovate automation |
