# AI Assistant Instructions

## Important Rules

1. **Always read project documentation** before starting work:

   - `README.md` — how to set up, run, and develop the project
   - `docs/references/code-practices.md` — code style, endpoint patterns, testing, logging conventions

2. **Before committing code changes**, run the same checks CI runs:

   - `uv run ruff format` (auto-fix formatting)
   - `uv run ruff check` (linting)
   - `uv run pyright` (type checking)
   - `uv run pytest` (tests)

## AI-Specific Behavior

### Comments

- Never reference internal issue tracker IDs (e.g., `GUM-123`) in code comments. This is a public repository and not everyone has access to our bug tracker
- Comments on fixes should include all relevant context inline so that the reasoning is self-contained

### Code Writing

- Only use emojis if the user explicitly requests it
- Never proactively create documentation files (\*.md) unless explicitly requested
- Always prefer editing existing files over creating new ones
- Always use `uv run` to execute Python commands (e.g., `uv run pytest`, `uv run python`). Never use bare `python` or `pip` — pyenv versions may not match `.python-version`
- When starting work outside of `/gumnut:start-linear-task`, always create a new branch from `main` before making changes. Don't modify files on an existing feature branch for unrelated work
- `services/` is for stateful classes with methods (stores, pipelines, WebSocket handlers). `utils/` is for stateless utility functions and helpers. Don't put classes with state in `utils/`.
- Accept specific parameters rather than the full `Settings` object in constructors. This keeps classes decoupled from the config layer and easier to test.

### Working with Files

- When editing a file, always read it first
- Never edit historical database migration files
- Place imports at the top of files (inline imports only to prevent circular dependencies)

### Datetime handling

- When working with datetimes as strings, ensure the proper format is used, as Immich has different formats for different use cases
- If you cannot determine the proper format to use, ask for clarification

For code style, endpoint patterns, error response format, testing, and logging conventions, see `docs/references/code-practices.md`.

### Immich Client Error Handling

- **Observed behavior:** Immich mobile and web clients have no HTTP 429 (rate limit) handling. A 429 causes sync failures, broken thumbnails, and upload errors with no automatic recovery.
- **Adapter contract:**
  - Never forward 429 responses from photos-api to Immich clients.
  - The Gumnut SDK (Stainless-generated) has built-in retry for 429, 5xx, and connection errors with exponential backoff, ±25% jitter, and `Retry-After` header support (see [SDK retry docs](https://www.stainless.com/docs/sdks/configure/client/#retries)). Configure `max_retries` on the client — **do not add a custom retry wrapper** on top, as it will stack with SDK retry and cause retry amplification.
  - `map_gumnut_error` must catch `RateLimitError` explicitly and return 502 (not 429) to Immich clients. The default error mapping would pass through the 429 status code.
- **Reference:** `docs/design-docs/request-overload-protection.md` in the `gumnut-dev-setup` repo.

## Sync Stream Architecture

The sync stream (`routers/api/sync/stream.py`) consumes events from photos-api and converts them to Immich sync format. Key concepts:

- **Two-phase ordering**: The stream yields all upserts first (in FK dependency order per `_SYNC_TYPE_ORDER`), then all deletes (in reverse FK order per `_DELETE_TYPE_ORDER`). This prevents FK constraint violations in the mobile client — parents exist before children reference them, and children are cleaned up before parents are removed. See `docs/design-docs/sync-stream-event-ordering.md` for the full design rationale and history.
- **Event types** are classified into `_DELETE_EVENT_TYPES` (construct delete sync event from event data), `_SKIPPED_EVENT_TYPES` (ignored), and everything else is treated as an upsert (fetch full entity from photos-api). Delete events are buffered during iteration and yielded in phase 2.
- **Deletion events** use `_make_delete_sync_event()` which maps `entity_id` to a UUID. For junction table deletions (e.g., `album_asset_removed`), the event's `payload` field carries the foreign keys since the record is hard-deleted
- **Face person_id handling**: `face_created` events have person_id nulled out (face detection never assigns a person). `face_updated` events use the causally-consistent person_id from the event payload instead of current entity state. After payload override, person_id is nulled if the person returned 404 during fetch (deleted entity) and no person checkpoint exists.
- **Album cover handling**: `album_updated` events use the causally-consistent `album_cover_asset_id` from the event payload instead of the entity's current computed cover (which is derived at fetch time via a lateral join and may reference an asset outside the sync window). After payload override, cover is nulled if the asset returned 404 during fetch and no asset checkpoint exists.
- **Adding a new version of an existing sync type** (e.g., AssetFacesV2 alongside V1): When the same gumnut entity type maps to multiple Immich sync versions, update these files in coordination:
  1. `stream.py`: Add V2 entry to `_SYNC_TYPE_ORDER` (after V1, same gumnut entity type). Add a guard in the stream loop to skip V1 when V2 is also requested (prevents duplicate events). Update face/entity-specific event handling to match both V1 and V2 sync entity types.
  2. `fk_integrity.py`: Add V2 to the entity's list in `_GUMNUT_TYPE_TO_SYNC_TYPES` so FK checkpoint lookups match regardless of which version was synced.
  3. `converters.py`: Write a V2 converter function alongside the V1 one.
  4. `events.py`: Update the converter dispatch in `convert_entity_to_sync_event` to select V1 vs V2 converter based on `sync_entity_type`.
  5. `test_sync_stream_ordering.py`: Verify the consistency test handles one-to-many gumnut-type-to-sync-type mappings.
- **No-op request types**: Immich sync types that are accepted but have no Gumnut equivalent (e.g., `AssetEditsV1` — we don't support editing) go in `_NOOP_REQUEST_TYPES` in `stream.py`. This prevents "unsupported type" warnings while making the no-op explicit. Do not just add them to `_SUPPORTED_REQUEST_TYPES` without `_SYNC_TYPE_ORDER` — that silently drops them.
- **Contract with photos-api**: The adapter depends on the events API response shape (`EventsResponse`). Fields like `payload` are typed in the SDK (v0.52.0+) and accessed directly. For backward compatibility with old events that predate a field, check for `None` before use
- **Debugging Immich mobile logs**: Immich mobile app logs contain Immich UUIDs, not Gumnut IDs. When debugging sync issues from mobile logs, use `routers/utils/gumnut_id_conversion.py` to convert UUIDs to Gumnut IDs (e.g., `face_`, `person_`, `asset_` prefixed) before looking up entities in production via API or MCP tools.

### Pull Requests

- When updating pull requests with additional commits, update the PR description to include the latest changes
- Always run tests and formatting before creating a PR

## Documentation Map

Detailed docs are in the `docs/` directory. Consult these when working in the relevant areas:

### Architecture

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Adapter architecture | `docs/architecture/adapter-architecture.md` | Overall adapter design, auth, data translation, pagination, sync protocol, error handling, endpoint status |
| WebSocket implementation | `docs/architecture/websocket-implementation.md` | WebSocket connections, real-time sync, event handling |
| Session & checkpoint implementation | `docs/architecture/session-checkpoint-implementation.md` | Session management, checkpoint tracking, sync state |

### Design Docs

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Immich auth architecture | `docs/design-docs/immich-auth-architecture.md` | Legacy auth design (deprecated, see auth-design.md) |
| Authentication design | `docs/design-docs/auth-design.md` | Current auth architecture, OAuth, token handling |
| Static file sharing | `docs/design-docs/static-file-sharing.md` | File sharing proposals, static asset serving |
| Render deploy with Docker | `docs/design-docs/render-deploy-docker.md` | Docker deployment, Render configuration |
| Checksum support | `docs/design-docs/checksum-support.md` | File integrity, checksum validation, deduplication |
| Sync stream event ordering | `docs/design-docs/sync-stream-event-ordering.md` | Sync FK integrity, event ordering, face/person deletion issues |

### Guides

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Running with Immich Web | `docs/guides/running-with-immich-web.md` | Setting up the full local stack (Immich web + adapter + photos-api + Clerk OAuth) |
| Running with Immich Mobile | `docs/guides/running-with-immich-mobile.md` | Self-signed certs, HTTPS setup, connecting the Immich mobile app |

### References

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| WebSocket events reference | `docs/references/websocket-events-reference.md` | WebSocket event types, payload formats |
| Session & checkpoint reference | `docs/references/session-checkpoint-reference.md` | Session/checkpoint object shapes, field definitions |
| Immich sync communication | `docs/references/immich-sync-communication.md` | Immich client-server sync protocol, message formats |
| Uvicorn settings | `docs/references/uvicorn-settings.md` | Server configuration, worker settings, timeouts |
| Code practices | `docs/references/code-practices.md` | Python style, endpoint patterns, testing, logging |
| Development tools | `docs/references/development-tools.md` | Model generation, API compatibility, OpenAPI spec |
