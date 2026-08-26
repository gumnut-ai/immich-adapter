---
title: "Routes, DTOs, and Upstream Compatibility"
last-updated: 2026-08-26
---

# Routes, DTOs, and Upstream Compatibility

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

- Use `Annotated` to specify attributes, such as `Query()`, `Path()`, `Body()` functions, or numeric or string validations, but do not use `Default` — the default value should be specified as part of the Python declaration. This is not just style: the `Query(default=X)` shape makes a param's default untestable — see [Testing](testing-and-logging.md#testing).
- If a parameter is not required, use `| SkipJsonSchema[None]` after defining the type to allow Pydantic to accept the `None` type, but prevent `None` from being exposed in the OpenAPI schema.
- If the exposed parameter name needs to be camelCase, use `alias="camelCase"` within the function and then use an appropriate snake_case name for the parameter in the function signature.

Example:
```python
asset_id: Annotated[UUID | SkipJsonSchema[None], Query(alias="assetId")] = None,
```

## Verifying upstream behavior — read the Immich source, not just the spec

The OpenAPI spec pins request/response *shapes*. It says nothing about the behavior clients actually depend on: which query params a given view sends, what the server filters or computes before answering, what the client does with a field once it has it. Adapter parity bugs live in that gap, and neither the spec nor recall closes it — check a local checkout of `immich-app/immich` at the tag in `.immich-container-tag`. Read it as `git show <tag>:<path>` rather than off the checkout's working tree, which tracks whatever version that clone was last left on and will answer for the wrong release without saying so (`git fetch --tags origin <tag>` first if it resolves). `server/src/repositories/` and `server/src/services/` for what the server computes; `web/src/lib/` and `web/src/routes/` for what the client sends and renders.

Generated models can also omit upstream zod refinements. Before synthesizing values, check the pinned upstream schema rather than relying only on `routers/immich_models.py`; edit-row UUID versions and rotation angles are examples.

Treat any claim about upstream behavior in a task description as a hypothesis until it is read there, including one you wrote yourself. The timeline stack work was specified on the premise that the Immich *client* collapses a burst using the per-asset tuple; upstream collapses server-side and the client only draws a badge, so building to the spec as written would have shipped a badge on every frame of every burst.

## Bumping the Immich Version

The Immich version the adapter targets is pinned in **two** files that must be kept in sync:

1. `.immich-container-tag` — read at runtime by `config/immich_version.py`, by `tools/generate_immich_models.py` when regenerating models from the OpenAPI spec, and by `scripts/extract-immich-web.py` when extracting web assets locally.
2. `Dockerfile`'s `ARG IMMICH_VERSION` — pulls `ghcr.io/immich-app/immich-server:${IMMICH_VERSION}` in the build stage to copy static web files into the image, and stamps the `immich.version` OCI label.

The two are not auto-synced, but CI enforces that they match (see the `check-immich-version-sync` job in `.github/workflows/ci.yml`). Render builds the image automatically from the repo without any way to inject a build-arg sourced from `.immich-container-tag`, so the Dockerfile default is what ships to production. When bumping the Immich version:

1. Update `.immich-container-tag`
2. Update the `ARG IMMICH_VERSION` default and the "Last updated" comment in `Dockerfile`
3. Regenerate `routers/immich_models.py` (see [development tools](development-tools.md))

Forgetting step 2 causes silent drift — the served web UI stays on the old Immich version while the API models advance.

A regen can add newly-required fields to (or retype) the generated DTOs, breaking endpoint stubs that hand-construct them at **runtime** (pydantic `ValidationError` → 500); these stubs have no callers in most tests, so the break hides until a client hits the route. Keep a construction smoke test per hand-built-DTO stub — `assert isinstance(await <endpoint>(), <Dto>)`, see `tests/unit/api/test_{system_config,jobs,license}.py` — and, as with an SDK bump, run the **full** `uv run pytest` after regenerating.

## Bumping the Gumnut SDK

The SDK is auto-generated (Stainless), so a version bump can add **newly-required** fields to response models (e.g. `FaceResponse.source` arrived in 0.116). Tests construct these models directly as fixtures, so a bump can break suites unrelated to the endpoint you're touching. Run the **full** `uv run pytest` after a bump (not just the changed endpoint's tests), and when a required field is added, `grep` the tests for `<Model>(` to fix every direct construction.

A bump can also *close* gaps silently: grep for raw `client.post(` / `client.delete(` call sites and migrate any whose typed method has now landed (see [Bulk-ID Endpoints](pagination-bulk-and-concurrency.md#bulk-id-endpoints)). Nothing fails to prompt this — a raw call keeps working forever.

Some SDK contracts are already pinned by committed tests, so a bump that breaks them fails the suite rather than waiting to be caught by hand. `tests/unit/utils/test_stack_conversion.py` does this for the stack resource — asserting each method still accepts the parameters the stack routes are being built to pass, and that each stack response class still carries the fields `GumnutStackRow` reads. Note what that guard does and doesn't cover: it catches a **renamed or dropped** parameter, not a newly-**required** one, so the full-suite run above is still what surfaces those. Extend the pattern when adding a surface whose SDK contract pyright can't check.

## Implementing New Endpoints

1. **Generate models**: Use `generate_immich_models.py` to create up-to-date Pydantic models (see [development tools](development-tools.md))
2. **Import models**: Use generated models from `routers.immich_models` for type safety
3. **Define parameters**: Follow the parameter conventions above
4. **Verify parameter semantics**: Check the Immich OpenAPI spec (`https://api.immich.app/endpoints/`) or source code (`immich/server/src/controllers/*.controller.ts` and the matching service) to confirm what each URL path and body parameter represents. URL `{id}` parameters don't always refer to the entity in the URL collection — face/person reassign endpoints in particular swap the natural reading. Both of these accept the **target person** as `{id}` in the path:
   - `PUT /people/{id}/reassign` — `{id}` is the target person (reassign TO); body items are sources.
   - `PUT /faces/{id}` — `{id}` is the target person (reassign TO); body `FaceDto.id` is the face being reassigned.
   - When fixing a path/body or ID-decoding bug in one handler, audit sibling handlers in the same router (and adjacent routers) for the same trap before closing the fix. A one-line search (`grep -rn` for the pattern) is cheap insurance against the same class-of-bug recurring.
   - The spec also can't express **response-shape guarantees** — array ordering, or which rows a collection field includes — and those live in the mapper and repository rather than the controller. At `v3.0.3`, `server/src/dtos/stack.dto.ts::mapStack` partitions so `assets[0]` is always the primary, and `server/src/repositories/stack.repository.ts` filters `deletedAt is null` so the array is live-only; clients depend on both. A field the spec types as a plain array can carry either guarantee, so read the mapper and repository for any collection field the adapter populates.
   - For loosely-specified response values (free-form strings, group/field names), also check how the Immich **clients** consume the response (`immich/web/src/` and `immich/mobile/lib/`) — clients hard-match values the OpenAPI spec doesn't constrain. E.g., the explore page renders only the group with `fieldName == "exifInfo.city"`, so a spec-valid response with different group names would silently render nothing.
   - The same duty applies to any **claim about client behavior you write down** — in a comment, docstring, test name, or doc — not just to implementation decisions. "Web passes `X`" / "no client reads this field" are plausible-sounding and frequently wrong: one param is typically passed differently across its call sites — `withHidden` is passed both ways, and omitted entirely by some callers — so what one caller does rarely generalizes. Grep the fork for **every** caller before asserting or refuting one, and don't commit the resulting tally: it rots on the next fork sync with nothing in CI pinning it.
   - The spec is **not the full route surface**: `@ApiExcludeEndpoint` routes are absent from OpenAPI, generated clients, and spec diffs. Check the controller when determining whether a route exists.
5. **Validate compatibility**: Run `validate_api_compatibility.py` to ensure correct implementation
6. **Test endpoints**: Verify responses match Immich API expectations
7. **Audit `/me/preferences` for a gating boolean**: Many client UI features (memories, tags, ratings, folders, people, shared links, email notifications, cast) are gated client-side on a flag in `UserPreferencesResponseDto`. The default in `routers/api/users.py::userPreferencesResponse` ships most of these as `enabled=False`, which silently hides the corresponding UI even after the backing endpoints are wired up. When implementing an endpoint that backs a client UI feature, grep `routers/api/users.py` for the matching preference field and flip its `enabled` to `True`. The Immich web client checks these via `$preferences?.<area>?.enabled`; missing the flip means the new endpoints become dead code on the client.
8. **Audit `routers/api/server.py::server_features`**: Many client UI features are also gated by a server-feature flag advertised via `GET /server/features`. When promoting an area from stub to a real implementation, flip the matching key from `False` to `True` and update the explanatory comment so it scopes only to the remaining stubbed sub-features. Leaving the flag at `False` after implementing the endpoint silently hides the UI; flipping it to `True` while parts of the area are still stubbed surfaces non-functional UI.
9. **Update the live-gap and architecture docs**: Promoting an endpoint from stub to real — **or dropping an endpoint removed upstream** — changes the active system record:
   - `docs/design-docs/immich-adapter-gap-analysis.md` — update or remove the affected row in **Live gaps**, then update **Priorities** when the work changes what should close next, be revisited, or remain intentional. Bump `last-updated`.
   - `docs/architecture/adapter-architecture.md` — update only when the endpoint changes a described system boundary, translation rule, collection shape, routing path, or failure behavior. This document is an architecture overview, not an endpoint catalog.
   - Search the feature name and removed path/handler across `docs/` and this reference for other summaries, rationale, feature-gate claims, or caller lists that the change makes false.

   If the endpoint emits a new WebSocket event or changes an existing event's timing, update the event's row in `docs/references/websocket-events-reference.md` **Summary Table** and its section under **Event Details**. Update `docs/architecture/websocket-implementation.md` only when room targeting, payload construction, delivery/failure semantics, or client convergence changes. Bump every edited document's `last-updated`.

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

Favorites are no longer an example of an unsupported filter. They map to the Gumnut rating dial's exact set `{5}` through `routers/utils/rating.py`; numeric rating filters map to their exact value, and an explicitly-present null rating maps to `{0}` (unrated). Apply the same rating to every data source a route composes: timeline counts and bucket listing, random-search counts and month pages, search statistics, asset statistics, and map marker listing. Filtering only one half creates internally inconsistent results even when each individual SDK call succeeds.

The deployed API accepts a `ratings` query parameter, but the generated Python SDK currently omits it from the typed list/count/search signatures. Use `rating_extra_query` for this narrow compatibility gap. When bumping the SDK, inspect those signatures and replace the shim once `ratings` is typed; do not leave both forms in one request.

This applies only to *restrictive* filters. Filters that ask for a broader result set (e.g., `withPartners=True` saying "also include partner-shared assets") can safely be dropped — the unfiltered result is a superset, not a wrong answer. Document in the docstring which filters are dropped and which short-circuit.

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

## Immich Client Error Handling

- **Observed behavior:** Immich mobile and web clients have no HTTP 429 (rate limit) handling. A 429 causes sync failures, broken thumbnails, and upload errors with no automatic recovery.
- **Adapter contract:**
  - Never forward 429 responses from the Gumnut API to Immich clients.
  - The Gumnut SDK (Stainless-generated) has built-in retry for 429, 5xx, and connection errors with exponential backoff, ±25% jitter, and `Retry-After` header support (see [SDK retry docs](https://www.stainless.com/docs/sdks/configure/client/#retries)). Configure `max_retries` on the client — **do not add a custom retry wrapper** on top, as it will stack with SDK retry and cause retry amplification.
  - The global `GumnutError` handler catches `RateLimitError` explicitly and returns 502 (not 429) to Immich clients. `map_gumnut_error` does the same when called directly from upload paths.
