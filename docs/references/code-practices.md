---
title: "Code Practices"
last-updated: 2026-04-24
---

# Code Practices

Style, patterns, and conventions for the immich-adapter codebase.

## Python Style Guide

- **Type Hints**: Use modern Python 3.12+ syntax (`int | None` instead of `Optional[int]`). Add type annotations to all function parameters and return types.
- **Naming**: Use `snake_case` for all variables, functions, and SQLAlchemy model attributes
- **Imports**: Always place imports at the top of files (inline imports only to prevent circular dependencies)
- **Dependencies**: Use `uv` for dependency management, not pip or poetry. Version dependencies appropriately in `pyproject.toml`.
- **Running Python**: Always use `uv run` to execute Python commands (e.g., `uv run pytest`, `uv run python`). Never use bare `python` or `pip` — pyenv versions may not match `.python-version`.

## Project Conventions

- **Comments**: Never reference internal issue tracker IDs (e.g., `GUM-123`) in code comments. This is a public repository and not everyone has access to our bug tracker. Comments on fixes should include all relevant context inline so that the reasoning is self-contained.
- **Module organization**: `services/` is for stateful classes with methods (stores, pipelines, WebSocket handlers). `utils/` is for stateless utility functions and helpers. Don't put classes with state in `utils/`.
- **Constructor parameters**: Accept specific parameters rather than the full `Settings` object in constructors. This keeps classes decoupled from the config layer and easier to test.
- **Branching**: Always create a new branch from `main` before making changes. Don't modify files on an existing feature branch for unrelated work.
- **File editing**: Always read a file before editing it. Never edit historical database migration files.
- **Datetime handling**: When working with datetimes as strings, ensure the proper format is used, as Immich has different formats for different use cases. If you cannot determine the proper format to use, ask for clarification.

## Immich API Integration

### HTTP Response Status Codes

Always use `fastapi.status` constants for `statusCode` — never use just the numeric value.

```python
# In route handlers:
raise HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Human-readable error description"
)

# Resulting JSON response:
# {"message": "...", "statusCode": 401, "error": "Unauthorized"}
```

### Error Response Format

All HTTP error responses must use Immich's expected format, not FastAPI's default:

```json
{
  "message": "Human-readable error description",
  "statusCode": 401,
  "error": "Unauthorized"
}
```

- `message`: Description of what went wrong
- `statusCode`: HTTP status code (duplicated in body for client convenience)
- `error`: HTTP status phrase (e.g., "Bad Request", "Unauthorized", "Internal Server Error")

This format is enforced by the global exception handler in `config/exceptions.py`. Raise `HTTPException` with a `detail` message and the handler will format it correctly.

**Note:** In middleware (e.g., `auth_middleware.py`), you must return `JSONResponse` directly with this format, as `HTTPException` raised in `BaseHTTPMiddleware.dispatch()` is not caught by FastAPI's exception handlers due to Starlette's middleware architecture.

For the full error handling strategy including rate limit protection and per-item error tracking, see the [adapter architecture doc](../architecture/adapter-architecture.md#error-handling).

### Defining Endpoint Parameters

- Use `Annotated` to specify attributes, such as `Query()`, `Path()`, `Body()` functions, or numeric or string validations, but do not use `Default` — the default value should be specified as part of the Python declaration.
- If a parameter is not required, use `| SkipJsonSchema[None]` after defining the type to allow Pydantic to accept the `None` type, but prevent `None` from being exposed in the OpenAPI schema.
- If the exposed parameter name needs to be camelCase, use `alias="camelCase"` within the function and then use an appropriate snake_case name for the parameter in the function signature.

Example:
```python
asset_id: Annotated[UUID | SkipJsonSchema[None], Query(alias="assetId")] = None,
```

### Bumping the Immich Version

The Immich version the adapter targets is pinned in **two** files that must be kept in sync:

1. `.immich-container-tag` — read at runtime by `config/immich_version.py`, by `tools/generate_immich_models.py` when regenerating models from the OpenAPI spec, and by `scripts/extract-immich-web.py` when extracting web assets locally.
2. `Dockerfile`'s `ARG IMMICH_VERSION` — pulls `ghcr.io/immich-app/immich-server:${IMMICH_VERSION}` in the build stage to copy static web files into the image, and stamps the `immich.version` OCI label.

The two are not auto-synced, but CI enforces that they match (see the `check-immich-version-sync` job in `.github/workflows/ci.yml`). Render builds the image automatically from the repo without any way to inject a build-arg sourced from `.immich-container-tag`, so the Dockerfile default is what ships to production. When bumping the Immich version:

1. Update `.immich-container-tag`
2. Update the `ARG IMMICH_VERSION` default and the "Last updated" comment in `Dockerfile`
3. Regenerate `routers/immich_models.py` (see [development tools](development-tools.md))

Forgetting step 2 causes silent drift — the served web UI stays on the old Immich version while the API models advance.

### Implementing New Endpoints

1. **Generate models**: Use `generate_immich_models.py` to create up-to-date Pydantic models (see [development tools](development-tools.md))
2. **Import models**: Use generated models from `routers.immich_models` for type safety
3. **Define parameters**: Follow the parameter conventions above
4. **Verify parameter semantics**: Check the Immich OpenAPI spec (`https://api.immich.app/endpoints/`) or source code (`immich/server/src/controllers/*.controller.ts` and the matching service) to confirm what each URL path and body parameter represents. URL `{id}` parameters don't always refer to the entity in the URL collection — face/person reassign endpoints in particular swap the natural reading. Both of these accept the **target person** as `{id}` in the path:
   - `PUT /people/{id}/reassign` — `{id}` is the target person (reassign TO); body items are sources.
   - `PUT /faces/{id}` — `{id}` is the target person (reassign TO); body `FaceDto.id` is the face being reassigned.
   - When fixing a path/body or ID-decoding bug in one handler, audit sibling handlers in the same router (and adjacent routers) for the same trap before closing the fix. A one-line search (`grep -rn` for the pattern) is cheap insurance against the same class-of-bug recurring.
5. **Validate compatibility**: Run `validate_api_compatibility.py` to ensure correct implementation
6. **Test endpoints**: Verify responses match Immich API expectations

### Exception Handling

- Don't expose implementation details in exceptions thrown to consumers
- Wrap low-level exceptions (e.g., Redis, HTTP client errors) in domain-specific exceptions
- Example: `SessionStore` catches `redis.exceptions.RedisError` and raises `SessionStoreError`

### Gumnut SDK Errors

The global handler in `config/exceptions.py` maps any `GumnutError` raised during request handling to an Immich-shaped JSON response, so most routes do **not** need to wrap SDK calls in `try/except`. Just call the SDK and let the error bubble:

```python
@router.get("/{id}")
async def get_album(id: UUID, client: AsyncGumnut = Depends(get_authenticated_gumnut_client)):
    return await client.albums.retrieve(uuid_to_gumnut_album_id(id))
```

The handler dispatches by isinstance against the typed Stainless exception hierarchy (`APIStatusError` subclasses → mapped status; `RateLimitError` → 502; `APIConnectionError` → 502; `APIResponseValidationError` → 502; generic `GumnutError` → 500).

For per-item handling inside bulk endpoints (where one failure shouldn't abort the batch), catch the specific typed exception and continue:

```python
for asset_uuid in request.ids:
    try:
        await client.assets.delete(uuid_to_gumnut_asset_id(asset_uuid))
    except NotFoundError:
        # Already gone; expected during sync.
        continue
    except APIStatusError as e:
        log_upstream_response(logger, ..., status_code=e.status_code, ...)
        continue
```

Use `map_gumnut_error(e, context, extra=..., exc_info=True)` only when the call site needs to enrich the upstream log record with context the global handler can't see — most commonly the upload paths logging filename / device ids / tracebacks.

### Counts and Aggregates

When a response only needs a count over a person's / album's assets, read the precomputed field off the parent entity rather than enumerating a paginator. `PersonResponse.asset_count` and `AlbumResponse.asset_count` are computed in O(1) by the Photos API and already trusted elsewhere in the adapter (e.g., `_immich_people_sort_key`, album conversion). Enumerating with `len([a async for a in client.assets.list(person_id=...)])` fans out into N paginated GETs of full asset payloads — this scaled to >10s on large persons (GUM-686).

Note that an `async for` paginator is always truthy: `if not client.assets.list(...)` is dead code, not an empty-list guard. The page contents are only known after iteration runs, so use the precomputed count rather than trying to short-circuit.

### Bulk-ID Endpoints

For backend endpoints that accept `{"ids": [...]}` (e.g., `POST /api/assets/trash`, `POST /api/assets/restore`, bulk `DELETE /api/assets`), chunk the request to stay under the backend's `MAX_BULK_GET_IDS=100` cap. Use the shared `BULK_CHUNK_SIZE` constant from `routers/utils/gumnut_client.py` and `itertools.batched`:

```python
from itertools import batched
from routers.utils.gumnut_client import BULK_CHUNK_SIZE

for chunk in batched(asset_uuids, BULK_CHUNK_SIZE):
    gumnut_ids = [uuid_to_gumnut_asset_id(uid) for uid in chunk]
    await client.post("/api/assets/trash", body={"ids": gumnut_ids}, cast_to=type(None))
```

Backend bulk endpoints are idempotent on already-transitioned rows (e.g., `trash_assets` skips already-trashed ids; `restore_assets` skips already-live ids). **Don't add per-id 404 / NotFoundError swallowing for these flows** — let bulk failures (validation, transport, 5xx) propagate to the global `GumnutError` handler. The per-id-loop-with-NotFoundError pattern shown above under *Gumnut SDK Errors* applies to single-asset endpoints (e.g., `client.assets.delete(asset_id)`), not to the bulk variants.

Pin the no-swallow contract with a `test_*_propagates_sdk_error` test per bulk flow — mock the bulk call to raise via `make_sdk_status_error(500, ...)` and assert `pytest.raises(APIStatusError)`. Without this test, a future refactor that wraps the bulk call in `try/except` would silently regress the contract. See `tests/unit/api/test_assets.py::TestDeleteAssets::test_delete_assets_force_false_propagates_sdk_error` for the canonical shape.

When the SDK doesn't yet expose a typed method for a backend endpoint (Stainless regenerates on a delay after each backend release), call the raw HTTP layer directly via `AsyncGumnut.post()` / `.delete()` with `cast_to=type(None)` for 204-returning endpoints:

```python
await client.post("/api/assets/trash", body={"ids": gumnut_ids}, cast_to=type(None))
await client.delete("/api/assets", body={"ids": gumnut_ids}, cast_to=type(None))
```

`AsyncGumnut` extends `AsyncAPIClient`, whose `.post()` / `.delete()` methods are public, route through the same JWT auth, retry, and response-hook plumbing as the typed methods, and surface the same `GumnutError` hierarchy. Don't import from `gumnut._types` — `cast_to=type(None)` works without it.

### WebSocket Emission

`emit_user_event` and `emit_session_event` (in `services/websockets.py`) are **fire-and-forget**: they catch `SocketIOError` from the underlying transport, log at WARN with `exc_info=True`, and return normally. **Do not wrap call sites in `try/except SocketIOError`** — the central swallow is the contract, and per-site catches are duplication. If the surrounding block needs to handle other exception types (e.g., DTO conversion before the emit, like `_emit_upload_events` in `routers/api/assets.py`), the broader try/except can stay; just don't add a separate `except SocketIOError` branch.

### Immich Client Error Handling

- **Observed behavior:** Immich mobile and web clients have no HTTP 429 (rate limit) handling. A 429 causes sync failures, broken thumbnails, and upload errors with no automatic recovery.
- **Adapter contract:**
  - Never forward 429 responses from photos-api to Immich clients.
  - The Gumnut SDK (Stainless-generated) has built-in retry for 429, 5xx, and connection errors with exponential backoff, ±25% jitter, and `Retry-After` header support (see [SDK retry docs](https://www.stainless.com/docs/sdks/configure/client/#retries)). Configure `max_retries` on the client — **do not add a custom retry wrapper** on top, as it will stack with SDK retry and cause retry amplification.
  - The global `GumnutError` handler catches `RateLimitError` explicitly and returns 502 (not 429) to Immich clients. `map_gumnut_error` does the same when called directly from upload paths.

## Testing

- All tests should be async and use `@pytest.mark.anyio` decorator
- Run tests from the project directory, not repository root
- Use model factories for test data creation
- Avoid asserting on logging in tests by default — logging is usually non-functional behavior. Exception: when log level itself is an explicit contract (for example, upstream status severity policy), assertions may verify level/metadata while avoiding brittle full-message matching.
- When mocking SDK paginator calls used with `async for` (e.g., `client.faces.list`), use `Mock(return_value=MockSyncCursorPage([...]))` — not `AsyncMock`. `AsyncMock` wraps the return in a coroutine, which breaks `async for` iteration. Use `AsyncMock` only for calls consumed with `await`.
- When mocking SDK response objects whose attributes are checked for truthiness (e.g., `if asset.metadata:`), explicitly set the attribute to its expected falsy value (`mock.metadata = None`). Unset Mock attributes return a truthy `Mock` object, silently flipping the branch and producing confusing downstream errors instead of clean `None`-path coverage. Audit `Mock`-based fixtures whenever an SDK field is renamed or added — grep across `tests/` for every `Mock()` construction of the relevant entity, including shared fixtures in `tests/conftest.py` and `tests/unit/api/sync/conftest.py` AND per-file inline mocks. Missing one is enough to silently flip a downstream Pydantic validation result.
- Do not add `__init__.py` to test directories — the project uses pytest's rootdir-based import resolution. Adding `__init__.py` switches pytest to package-based imports, breaking test discovery.

## Logging

Use structured logging with key/value metadata in the `extra` dict. Include relevant identifiers for traceability.

```python
logger.info(f"Created library {library.id}", extra={"library_id": library.id})
logger.info("WebSocket connected", extra={"sid": sid, "user_id": user_id, "device_type": session.device_type})
```

This enables better searching and correlation in Sentry.

### Upstream response log levels

For responses/errors from upstream photos-api/Gumnut calls, use status-based severity:

- `404` → `INFO`
- Other `4xx` (including `400`, `401`, `403`, `422`, `429`) → `WARNING`
- `5xx` → `ERROR`

When possible, use shared helpers in `routers/utils/error_mapping.py` (`upstream_status_log_level` / `log_upstream_response`) instead of ad-hoc `if/else` logging branches.

**Reserved `extra` keys**: Python's `LogRecord` has reserved attributes (`filename`, `module`, `name`, `msg`, `args`, `levelname`, `pathname`, `lineno`, etc.). Using these as `extra` keys causes a `KeyError` at runtime. Use prefixed names instead (e.g., `upload_filename` instead of `filename`).

## Pull Requests

- When updating pull requests with additional commits, update the PR description to include the latest changes
- Always run tests and formatting before creating a PR
