---
title: "Route Errors and Filters"
last-updated: 2026-10-01
---

# Route Errors and Filters

## Stub endpoints — fail closed on auth/authz checks

The adapter has many stub endpoints (PIN code, session lock/unlock, change-password, etc.) that intentionally return success without doing real work, because Immich clients call them and expect a 2xx but the adapter doesn't model the underlying feature. That pattern is fine for purely informational stubs, but **don't apply it to endpoints whose contract is "tell the caller whether the request is authenticated/authorized"**. The Immich client trusts those answers — `auth_guard.dart` calls `/api/auth/validateToken` on app launch and navigation, lets the user past the login gate when the response is `authStatus=true`, and bounces to the login screen on a 401. A handler that always returns `True` lets clients with a missing *or expired* token past the gate, and the failure only surfaces on the next real API call (presenting as a sudden mid-session expiry rather than a missing-credential problem).

Rule of thumb: a stub may safely return success when it represents a feature the adapter doesn't implement. A handler that gates on auth must reflect the *real* token state. Checking that `request.state.jwt_token` is merely present is **not** enough — the client's session token can outlive its stored JWT, so the JWT can be present but expired. `routers/api/auth.py::validate_access_token` therefore takes `Depends(get_authenticated_gumnut_client)` (which 401s when no JWT is present) and probes the backend with `await client.users.me()`: an expired JWT surfaces as a 401 via the global `GumnutError` handler, and a still-refreshable one is renewed transparently by the auth middleware. Use that pattern when the answer must reflect live token validity; a bare `getattr(request.state, "jwt_token", None)` presence check + `HTTPException(401, ...)` only rejects the no-credential case and will happily pass a stale token.

## Restrictive filters the backend can't honor — short-circuit, don't drop

When an Immich endpoint accepts query filters that Gumnut doesn't model (e.g., `isArchived` or locked visibility), silently dropping the filter and returning unfiltered results is a wrong answer — the client asked to *restrict* results and got everything instead. For filters with that semantic, short-circuit to `[]` when the restrictive value is set:

```python
if isArchived is True:
    return []
```

Use `is True` rather than truthiness so `False` / `None` (which mean "no restriction") still return normal results — only the explicit `True` value asked for filtering. `routers/api/map.py::get_map_markers` retains this pattern for archived state.

Favorites are no longer an example of an unsupported filter. Through `routers/utils/rating.py`, explicit true maps to exact set `{5}`, explicit false maps to `{0,1,2,3,4}`, numeric rating filters map to their exact value, and an explicitly-present null rating maps to `{0}` (unrated). Omission alone means no favorite/rating restriction. Apply the same set to every data source a route composes: timeline counts and bucket listing, random-search counts and month pages, search statistics, asset statistics, and map marker listing. Filtering only one half creates internally inconsistent results even when each individual SDK call succeeds.

The deployed API accepts a `ratings` query parameter, but the generated Python SDK currently omits it from the typed list/count/search signatures. Use `rating_extra_query` for this narrow compatibility gap. When bumping the SDK, inspect those signatures and replace the shim once `ratings` is typed; do not leave both forms in one request.

This rule applies to *restrictive* filters. The map route accepts and drops
unsupported broadening flags such as `withPartners=True` and
`withSharedAlbums=True`; its response contains the caller's assets without the
additional shared assets. That compatibility gap yields a subset of the
broader request, whereas ignoring a restrictive filter adds rows the caller
excluded. Document in the docstring which filters are dropped and which
short-circuit.

## Exception Handling

- Don't expose implementation details in exceptions thrown to consumers
- Wrap low-level exceptions (e.g., Redis, HTTP client errors) in domain-specific exceptions
- Example: `SessionStore` catches `redis.exceptions.RedisError` and raises `SessionStoreError`

## Gumnut SDK Errors

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

**Streaming responses.** Once a `StreamingResponse` commits its headers,
generator exceptions cannot reach the global error handler. Resolve
authentication and setup errors before returning the response. Degrade a
per-item failure only when skipping cannot advance a cursor past data that later
items depend on; otherwise propagate it so the stream truncates and retries.
Keep guards narrow: `ValidationError` subclasses `ValueError`, so catch decoding
failures around the decode call rather than around model construction.

## Immich Client Error Handling

- **Observed behavior:** Immich mobile and web clients have no HTTP 429 (rate limit) handling. A 429 causes sync failures, broken thumbnails, and upload errors with no automatic recovery.
- **Adapter contract:**
  - Never forward 429 responses from the Gumnut API to Immich clients.
  - The Gumnut SDK (Stainless-generated) has built-in retry for 429, 5xx, and connection errors with exponential backoff, ±25% jitter, and `Retry-After` header support (see [SDK retry docs](https://www.stainless.com/docs/sdks/configure/client/#retries)). Configure `max_retries` on the client — **do not add a custom retry wrapper** on top, as it will stack with SDK retry and cause retry amplification.
  - The global `GumnutError` handler catches `RateLimitError` explicitly and returns 502 (not 429) to Immich clients. `map_gumnut_error` does the same when called directly from upload paths.
