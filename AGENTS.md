# AI Assistant Instructions

## Important Rules

1. **Always read README.md files** in each project directory to understand:

   - How to set up and run the project
   - Available commands and scripts
   - Code style and conventions
   - Testing requirements

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

### Working with Files

- When editing a file, always read it first
- Never edit historical database migration files
- Place imports at the top of files (inline imports only to prevent circular dependencies)

### Datetime handling

- When working with datetimes as strings, ensure the proper format is used, as Immich has different formats for different use cases
- If you cannot determine the proper format to use, ask for clarification

### Exception Handling

- Don't expose implementation details in exceptions thrown to consumers
- Wrap low-level exceptions (e.g., Redis, HTTP client errors) in domain-specific exceptions
- Example: `SessionStore` catches `redis.exceptions.RedisError` and raises `SessionStoreError`

### Type Annotations

- Add type annotations to all function parameters and return types

### Logging

- Always use structured logging with key/value metadata in the `extra` dict
- Include relevant identifiers for traceability: `user_id`, `session_token`, `sid`, `asset_id`, etc.
- Example: `logger.info("WebSocket connected", extra={"sid": sid, "user_id": user_id, "device_type": session.device_type})`
- **Do not assert on logging in tests.** Logging is non-functional behavior — tests should assert on observable outputs (return values, side effects, emitted events), not on whether a particular log message was emitted

### HTTP Response Status Codes

- Always use `fastapi.status` constants for `statusCode` - never use just the numeric value

```python
# In route handlers:
raise HTTPException(
   status_code=status.HTTP_401_UNAUTHORIZED,
   detail="Human-readable error description"
)

# Resulting JSON response:
# {"message": "...", "statusCode": 401, "error": "Unauthorized"}
```

### Error Responses

All HTTP errors must use Immich's expected format:

```json
{
  "message": "Human-readable error description",
  "statusCode": 401,
  "error": "Unauthorized"
}
```

- In route handlers: Raise `HTTPException(status_code=..., detail="...")` - the global handler formats it
- In middleware: Return `JSONResponse` directly with the above format (HTTPException doesn't work in BaseHTTPMiddleware)

## Sync Stream Architecture

The sync stream (`routers/api/sync/stream.py`) consumes events from photos-api and converts them to Immich sync format. Key concepts:

- **Two-phase ordering**: The stream yields all upserts first (in FK dependency order per `_SYNC_TYPE_ORDER`), then all deletes (in reverse FK order per `_DELETE_TYPE_ORDER`). This prevents FK constraint violations in the mobile client — parents exist before children reference them, and children are cleaned up before parents are removed. See `docs/design-docs/sync-stream-event-ordering.md` for the full design rationale and history.
- **Event types** are classified into `_DELETE_EVENT_TYPES` (construct delete sync event from event data), `_SKIPPED_EVENT_TYPES` (ignored), and everything else is treated as an upsert (fetch full entity from photos-api). Delete events are buffered during iteration and yielded in phase 2.
- **Deletion events** use `_make_delete_sync_event()` which maps `entity_id` to a UUID. For junction table deletions (e.g., `album_asset_removed`), the event's `payload` field carries the foreign keys since the record is hard-deleted
- **Face person_id handling**: `face_created` events have person_id nulled out (face detection never assigns a person). `face_updated` events use the causally-consistent person_id from the event payload instead of current entity state. After payload override, person_id is nulled if the person returned 404 during fetch (deleted entity) and no person checkpoint exists.
- **Album cover handling**: `album_updated` events use the causally-consistent `album_cover_asset_id` from the event payload instead of the entity's current computed cover (which is derived at fetch time via a lateral join and may reference an asset outside the sync window). After payload override, cover is nulled if the asset returned 404 during fetch and no asset checkpoint exists.
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
| Adapter architecture | `docs/architecture/adapter-architecture.md` | Understanding overall adapter design, request flow, middleware |
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

### Getting Started

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| Running with Immich Web | `README.md` § "Running with Immich Web" | Setting up the full local stack (Immich web + adapter + photos-api + Clerk OAuth) |
| Running with Immich Mobile | `README.md` § "Running with Immich Mobile" | Self-signed certs, HTTPS setup, connecting the Immich mobile app |

### References

| Topic | Document | Consult when... |
|-------|----------|-----------------|
| WebSocket events reference | `docs/references/websocket-events-reference.md` | WebSocket event types, payload formats |
| Session & checkpoint reference | `docs/references/session-checkpoint-reference.md` | Session/checkpoint object shapes, field definitions |
| Immich sync communication | `docs/references/immich-sync-communication.md` | Immich client-server sync protocol, message formats |
| Uvicorn settings | `docs/references/uvicorn-settings.md` | Server configuration, worker settings, timeouts |
