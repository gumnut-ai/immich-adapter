---
title: "Route and DTO Contracts"
last-updated: 2026-10-01
---

# Route and DTO Contracts

## HTTP Response Status Codes

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

## Error Response Format

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

**Passing per-request state from a handler back up to the middleware:** a `ContextVar.set()` inside the downstream handler does **not** propagate back to `dispatch()` after `call_next` (same Starlette `BaseHTTPMiddleware` boundary). Never use process-global / module-level mutable state (a bare `ContextVar`, `threading.local`, etc.) to carry per-request values — under concurrent load one request can read another's value, which for credentials means cross-user contamination. Install a per-request mutable holder on a `ContextVar` in `dispatch()` *before* `call_next`, and have the handler mutate that object (see `gumnut_client.py` refreshed-token holder).

For the full error handling strategy including rate limit protection and per-item error tracking, see the [adapter architecture doc](../architecture/adapter-architecture.md#failure-behavior).

## Defining Endpoint Parameters

- Use `Annotated` to specify attributes, such as `Query()`, `Path()`, `Body()` functions, or numeric or string validations, but do not use `Default` — the default value should be specified as part of the Python declaration. This is not just style: the `Query(default=X)` shape makes a param's default untestable — see [Testing](testing.md).
- If a parameter is not required, use `| SkipJsonSchema[None]` after defining the type to allow Pydantic to accept the `None` type, but prevent `None` from being exposed in the OpenAPI schema.
- If the exposed parameter name needs to be camelCase, use `alias="camelCase"` within the function and then use an appropriate snake_case name for the parameter in the function signature.

Example:
```python
asset_id: Annotated[UUID | SkipJsonSchema[None], Query(alias="assetId")] = None,
```

## Omit vs explicit-null in update-style DTOs — use `model_fields_set`

Many generated Immich update DTOs declare each field as `T | None = None` (e.g., `UpdateAssetDto`'s `description`, `latitude`, `longitude`, `dateTimeOriginal`). On the wire, Immich clients distinguish two different intents:

- **Omitted** (`{}` or no key for the field) — "leave this field unchanged."
- **Explicit null** (`{"description": null}`) — "clear this field."

Both arrive at the model as `None` because the default is `None`. To disambiguate, read **`request.model_fields_set`** (Pydantic v2). It records the set of field names that were present in the input JSON, independent of their value:

```python
provided = request.model_fields_set
patch: dict[str, Any] = {}
if "description" in provided:
    patch["description"] = request.description  # may be None — that's "clear"
# Fields not in `provided` are omitted from the patch entirely so the SDK's
# `Omit` sentinel default applies.
```

When the SDK method accepts `Omit | None | T`, leaving the kwarg out of the `**patch` unpack maps cleanly to "leave unchanged"; including it with `None` maps to "clear." Without `model_fields_set`, the adapter can only see `None` and conflates the two intents.

This pattern is needed wherever the upstream Immich DTO uses `T | None = None` defaults AND the backend (or wire contract) distinguishes "unset" from "cleared." Bulk-update DTOs, single-asset edit, person/album edits — audit each new update endpoint for this trap before forwarding the DTO to the SDK.

## Mobile-client null-aware string parsing

The Immich mobile app (Dart) parses some response fields with the null-aware `?.` operator — for example, `response.assets.nextPage?.toInt()` in the search service. Dart's `?.` short-circuits **only on `null`**, not on empty string. Returning `""` instead of `None` for an `Optional[str]` field whose mobile-side parser is `?.toInt()` / `?.toDouble()` crashes the client with `FormatException` on every successful response. Use `None` as the sentinel for any optional string the mobile client may parse numerically.

Concrete example: `SearchResponseDto.assets.nextPage` is typed `str | None` in the generated model; the adapter previously emitted `""`, which made every successful `/api/search/metadata` and `/api/search/smart` response crash the Android client. Audit any `Optional[str]` response field whose upstream Dart usage pattern is `?.<numeric-parse>()`.


## Immich web today wire format

- **Immich web "today" wire format**: Some query params receive a string produced by the web client's `asLocalTimeISO`, which does `setZone('utc', { keepLocalTime: true })`. The wire value's date and time components are the user's **local wall-clock**, with `Z` appended so it transports as a string — the offset is fictitious. Pull `.year/.month/.day/.hour/.minute` off the parsed datetime as-is; do **not** apply timezone math, or you'll shift the user's local "today" by their UTC offset. As of Immich v3.2.0 the client still applies the hack to the search filter's `takenAfter`/`takenBefore`, which `POST /search/metadata` forwards to the Gumnut API as `local_datetime_after`/`local_datetime_before` (`routers/api/search.py`). Note that forwarding is a deliberate translation, not an identity: Immich resolves `takenAfter`/`takenBefore` against `fileCreatedAt`, a different field from the Gumnut API's `local_datetime`. `GET /memories?for=` was the canonical example until v3.0.3 retyped the param to a plain `date` — see `_local_today` in `routers/api/memories.py` for why that param stays typed `datetime`. The hack may reappear on any endpoint where the client wants the server to interpret a value in the user's local time without exposing the offset.
