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

- **Event types** are classified into `_DELETE_EVENT_TYPES` (construct delete sync event from event data), `_SKIPPED_EVENT_TYPES` (ignored), and everything else is treated as an upsert (fetch full entity from photos-api)
- **Deletion events** use `_make_delete_sync_event()` which maps `entity_id` to a UUID. For junction table deletions (e.g., `album_asset_removed`), the event's `payload` field carries the foreign keys since the record is hard-deleted
- **Contract with photos-api**: The adapter depends on the events API response shape (`EventsResponse`). Fields like `payload` are typed in the SDK (v0.52.0+) and accessed directly. For backward compatibility with old events that predate a field, check for `None` before use

### Pull Requests

- When updating pull requests with additional commits, update the PR description to include the latest changes
- Always run tests and formatting before creating a PR
