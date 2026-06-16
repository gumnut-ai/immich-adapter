---
title: "Code Practices"
last-updated: 2026-06-15
---

# Code Practices

Style, patterns, and conventions for the immich-adapter codebase.

## Python Style Guide

- **Type Hints**: Use modern Python 3.12+ syntax (`int | None` instead of `Optional[int]`). Add type annotations to all function parameters and return types.
- **Type narrowing — overloads, not asserts**: When a helper returns `T | None` but a specific call site is guaranteed to receive non-None input, narrow with `@overload` decorators on the helper (`@overload def f(x: T) -> T; @overload def f(x: None) -> None; @overload def f(x: T | None) -> T | None`) rather than `assert x is not None` at the call site. Asserts are stripped under `python -O`, obscure whether the None branch is actually reachable, and only narrow at one site instead of helping every caller. See `to_actual_utc` / `to_immich_local_datetime` in `routers/utils/datetime_utils.py` for the pattern. For genuine runtime defense (input that *can* be invalid), use exceptions, not `assert`.
- **Named types over bare tuples**: When a function returns multiple values whose positional meaning is ambiguous (e.g. two `int | None` a caller could transpose), return a small `NamedTuple` (or dataclass) with named fields and a docstring rather than a bare `tuple[...]`. Named fields make call sites self-documenting and let the type carry what `None` means. See `ImmichUserQuota` / `map_user_quota` in `routers/utils/current_user.py`.
- **Naming**: Use `snake_case` for all variables, functions, and SQLAlchemy model attributes
- **Imports**: Always place imports at the top of files (inline imports only to prevent circular dependencies)
- **Dependencies**: Use `uv` for dependency management, not pip or poetry. Version dependencies appropriately in `pyproject.toml`.
- **Running Python**: Always use `uv run` to execute Python commands (e.g., `uv run pytest`, `uv run python`). Never use bare `python` or `pip` — pyenv versions may not match `.python-version`.

## Project Conventions

- **Single source of truth for rationale**: explain a non-obvious *why* once; cite it elsewhere in one line rather than restating (copies multiply churn and drift). Home by scope: one symbol → its docstring; spanning files → one reference doc; a decision → the design doc. Never link to a doc *and* restate it.
- **Comments earn their place**: explain what the code can't. Delete the road not taken ("did X not Y", unless Y is a trap someone will re-reach for), a "today" coincidence ("A equals B today" — enforce it in code if it's load-bearing, else cut it), and prose restating what the structure already shows (an explicit dict entry already means "explicit, not the default").
- **Committed text — public names only, no internal references**: This is a public repository, so everything committed (code comments, docstrings, test names and docstrings, design docs under `docs/design-docs/`, `docs/architecture/`, `docs/references/`, `docs/guides/`, plus PR bodies and commit messages) must read cleanly for an external reader who can't see anything private. Three things to keep out:
  - **The backend's internal name.** Refer to the backend as **the Gumnut API** (its public name, `api.gumnut.ai`) — or "the Gumnut backend" where a generic phrasing reads better — never `photos-api`, the internal project/repo name (it lives in the private `gumnut-ai/photos` repo). This applies to comments, docstrings, tests, and docs alike. Identifiers that embed the internal name follow the same rule (e.g., the `GUMNUT_API_MAX_PAGE_SIZE` constant, not `PHOTOS_API_MAX_PAGE_SIZE`).
  - **Private repo file-paths.** Don't cite paths into private repos (e.g., `photos-api/routers/...`); describe the contract or behavior instead, so the note stays useful to a reader who can't open that file.
  - **Internal issue-tracker IDs** (e.g., `GUM-123`). External readers cannot resolve them and they leak the existence of internal work-tracking. Inline the rationale instead so the reasoning is self-contained (e.g., "the recently shipped end-to-end Range path" rather than "GUM-713").

  Existing references predate this rule; sweep them out opportunistically when editing nearby content. (The literal `photos-api` / `GUM-123` tokens in this bullet are deliberate examples of what to avoid.)
- **Module organization**: `services/` is for stateful classes with methods (stores, pipelines, WebSocket handlers). `utils/` is for stateless utility functions and helpers. Don't put classes with state in `utils/`.
- **Constructor parameters**: Accept specific parameters rather than the full `Settings` object in constructors. This keeps classes decoupled from the config layer and easier to test.
- **Branching**: Always create a new branch from `main` before making changes. Don't modify files on an existing feature branch for unrelated work.
- **File editing**: Always read a file before editing it. Never edit historical database migration files.
- **Datetime handling**: When working with datetimes as strings, ensure the proper format is used, as Immich has different formats for different use cases. If you cannot determine the proper format to use, ask for clarification.
- **Asset date fields**: Any endpoint or converter that emits Immich asset date fields must use the shared helpers in `routers/utils/asset_conversion.py`:
  - `resolve_capture_datetime`, `resolve_file_created_at`, `resolve_local_date_time` — for capture-time fields. The Gumnut API resolves `asset.local_datetime` from `metadata.original_datetime → file_created_at → created_at` internally, so the helpers trust it as the single source of truth and the adapter must not re-add a fallback chain. The helpers then handle Immich's actual-UTC `fileCreatedAt` and keep-local-time `localDateTime` formats.
  - `resolve_file_modified_at` — for the `fileModifiedAt` field. Unlike capture time, the Gumnut API does not resolve a single modify-time field — `asset.file_modified_at` is the raw file mtime — so the helper applies a `metadata.modified_datetime → asset.file_modified_at` cascade here. Do not "align" this with the capture-time helpers; the asymmetry is deliberate.
- **Immich web "today" wire format**: Endpoints that take a "today" or "now" query param (e.g., `GET /memories?for=...`) receive a string produced by the web client's `asLocalTimeISO`, which does `setZone('utc', { keepLocalTime: true })`. The wire value's date and time components are the user's **local wall-clock**, with `Z` appended so it transports as a string — the offset is fictitious. Pull `.year/.month/.day/.hour/.minute` off the parsed datetime as-is; do **not** apply timezone math, or you'll shift the user's local "today" by their UTC offset. The same hack may appear on any future endpoint where the client wants the server to interpret a value in the user's local time without exposing the offset.

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

**Passing per-request state from a handler back up to the middleware:** a `ContextVar.set()` inside the downstream handler does **not** propagate back to `dispatch()` after `call_next` (same Starlette `BaseHTTPMiddleware` boundary). Never use process-global / module-level mutable state (a bare `ContextVar`, `threading.local`, etc.) to carry per-request values — under concurrent load one request can read another's value, which for credentials means cross-user contamination. Install a per-request mutable holder on a `ContextVar` in `dispatch()` *before* `call_next`, and have the handler mutate that object (see `gumnut_client.py` refreshed-token holder).

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

### Bumping the Gumnut SDK

The SDK is auto-generated (Stainless), so a version bump can add **newly-required** fields to response models (e.g. `FaceResponse.source` arrived in 0.116). Tests construct these models directly as fixtures, so a bump can break suites unrelated to the endpoint you're touching. Run the **full** `uv run pytest` after a bump (not just the changed endpoint's tests), and when a required field is added, `grep` the tests for `<Model>(` to fix every direct construction.

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
7. **Audit `/me/preferences` for a gating boolean**: Many client UI features (memories, tags, ratings, folders, people, shared links, email notifications, cast) are gated client-side on a flag in `UserPreferencesResponseDto`. The default in `routers/api/users.py::userPreferencesResponse` ships most of these as `enabled=False`, which silently hides the corresponding UI even after the backing endpoints are wired up. When implementing an endpoint that backs a client UI feature, grep `routers/api/users.py` for the matching preference field and flip its `enabled` to `True`. The Immich web client checks these via `$preferences?.<area>?.enabled`; missing the flip means the new endpoints become dead code on the client.
8. **Audit `routers/api/server.py::server_features`**: Many client UI features are also gated by a server-feature flag advertised via `GET /server/features`. When promoting an area from stub to a real implementation, flip the matching key from `False` to `True` and update the explanatory comment so it scopes only to the remaining stubbed sub-features. Leaving the flag at `False` after implementing the endpoint silently hides the UI; flipping it to `True` while parts of the area are still stubbed surfaces non-functional UI.
9. **Update the implementation-status docs**: Promoting an endpoint from stub to real changes two evergreen docs that the docs-as-system-of-record convention requires keeping current:
   - `docs/architecture/adapter-architecture.md` — move the row from the "Stub implementations" table to "Fully implemented" (or split into a partial-implementation row, mirroring "Memories (read)" / "Memories (write)"); bump `last-updated` in the frontmatter.
   - `docs/design-docs/immich-adapter-gap-analysis.md` — flip the gap section to **Closed**, update the summary stub count, and strike the entry from the Tier-1/2/3 plan table; bump `last-updated`.

   Skipping either leaves future readers (and gap-prioritization passes) reasoning from stale data — the gap-analysis doc explicitly carries `status: active`, so consistency with the implementation is part of its contract.

   After updating the dedicated sections and tables, also grep each touched doc for **other paragraphs that summarize the prior state** — design-decision notes, recommendations, server-feature flag rollups, etc. The gap-analysis doc in particular has a `GET /server/features` design-decision paragraph that enumerates which client UI flags are on/off; flipping a server-feature flag in the code without updating that paragraph leaves it contradicting both the code and the gap section you just updated. Search for the feature name (e.g., `map`, `trash`, `duplicateDetection`) across the doc before considering the update done.

   If the new endpoint also emits a WebSocket event (existing one or new), also update **both** websocket docs and bump their `last-updated`:
   - `docs/architecture/websocket-implementation.md` — move the event out of the "Not Applicable" table into the Phase 1 supported table (or add a new row), with the payload, Web/Mobile columns, and a Notes value that names the emit site.
   - `docs/references/websocket-events-reference.md` — update the Summary Table row and the per-event section. Upstream-Immich and adapter triggers may diverge (e.g., upstream emits `on_asset_update` only from sidecar processing; the adapter emits it from `PUT /api/assets/{id}`); make both paths explicit so future readers don't assume the upstream-only note still applies.

### Reading Gumnut asset fields — request them via `include`

The Gumnut API is moving the asset response to a **lean default** behind a JSON:API-style `?include=` parameter: an omitted `include` will eventually return none of the heavy fields, and several previously-required fields (`faces`, `people`, and the `file_data` scalars `device_asset_id` / `device_id` / `file_created_at` / `file_modified_at` / `checksum` / `file_size_bytes`) are already nullable. So **any** `client.assets.list` / `client.search.search` / `client.assets.retrieve` whose result feeds a conversion that reads `metadata`, `people`, or a `file_data` scalar must pass the matching `include` — otherwise, once the default flips, those fields arrive `null` and the Immich asset is silently corrupted (empty checksum, null size/EXIF, missing people). The full-default transition window hides the bug until the flip, so it won't surface in tests against today's backend.

Use the constants in `routers/utils/asset_conversion.py`, chosen by what the conversion **downstream of the call** actually reads:

| Constant | Tokens | Use for |
|----------|--------|---------|
| `ASSET_INCLUDE` | `metadata, people, file_data` | Reads feeding `convert_gumnut_asset_to_immich` (it emits `people`): `get_asset_info`, search, albums, memories, full/delta sync, the upload-success retrieve. |
| `ASSET_INCLUDE_NO_PEOPLE` | `metadata, file_data` | The sync-stream `entity_fetch` reads, whose converters read the `file_data` scalars but never `people`. |
| `ASSET_INCLUDE_METADATA_ONLY` | `metadata` | Reads that touch only `metadata`: map markers (GPS), the bulk per-asset datetime rewrite (`original_datetime`). |

Reads that consume only **lean-core** fields (`id`, `mime_type`, `width`/`height`, `duration`, `trashed_at`, `local_datetime`) or `asset_urls` request **no** `include` — those stay populated regardless (today: timeline buckets, trash-id collection, asset-count stats, the image-serving `_retrieve_and_stream_variant`, and the `/faces` width/height read). `asset_urls` is **not** gated by `include`, so the streaming/serving paths never need one, and `faces` is never requested off the asset — the adapter reads faces from the dedicated `/faces` endpoint. The `create()` (buffered upload) and `update_asset()` (PATCH) responses keep the full shape and expose no `include` param, so they need no change.

Bumping `gumnut-sdk` can itself relax a previously-non-null asset field to `| None` as part of this migration (e.g. `file_modified_at` went `datetime` → `datetime | None`), which then needs a null-guard at every read site (`resolve_file_modified_at` falls back through `metadata.modified_datetime → file_data.file_modified_at → capture time` so the required Immich `fileModifiedAt` is never null). Run `uv run pyright` after any `gumnut-sdk` bump to surface newly-required guards.

**Read the file/provenance scalars from the nested `file_data` object, not the deprecated flat top-level fields.** The Gumnut API exposes the group in two shapes under the same `include=file_data` gate: the preferred nested `gumnut_asset.file_data` object (`checksum_sha1`, `file_modified_at`, `file_size_bytes`, `device_asset_id`, `device_id`, `file_created_at`, `checksum`) and the equivalent flat top-level scalars (`gumnut_asset.checksum_sha1`, …). The flat scalars are deprecated and being removed, so read `gumnut_asset.file_data.<field>` guarding `file_data is None` — `file_data` is `None` when `include=file_data` isn't requested, and that guard preserves the legacy fallbacks (empty Immich checksum on a null `checksum_sha1`, null size, the modify-time cascade above). The whole group is gated by the `file_data` include token either way.

At **runtime**, a field the server omits — an older Gumnut API during a rollout, or a field gated behind an `include` the call didn't request — does **not** raise `AttributeError` on access. The SDK builds responses with non-validating construction (`construct_type`, since `_strict_response_validation` is off), which materializes every field the model declares and defaults an omitted one to `None`. So read such a field with plain attribute access and treat `None` as "absent"; a `getattr(obj, "field", default)` guard written to catch an `AttributeError` fallback is dead code (the attribute is always present), and a docstring claiming the access can raise misleads the next reader. `AttributeError` is reachable only when the *pinned* SDK model doesn't declare the field at all — which the version pin precludes — so verify the installed model actually exposes a field (e.g. `grep` the installed `gumnut/types/*.py`) rather than trusting a version number, since a Stainless commit's internal `version` string can differ from the published release that first ships the field.

### Asset dimensions and orientation

The Gumnut API owns display-space dims at ingest — `asset.width` / `asset.height` already reflect post-rotation dimensions and must be emitted **verbatim** on the wire (immich web reads them via `getAssetRatio`). Pre-rotation raw dims live on `metadata.raw_width` / `metadata.raw_height`; surface them on `exifInfo.exifImageWidth` / `exifImageHeight` so Immich mobile can re-derive display dims locally. When raw dims are present, the EXIF `orientation` tag is emitted unchanged — mobile pairs it with the raw dims to compute display dims; immich web ignores it (it reads `asset.width/height` directly).

Use `exif_dims_and_orientation(gumnut_asset)` from `routers/utils/asset_conversion.py` at every emit site that populates `exifInfo.exifImageWidth/Height` and the EXIF `orientation` field. The helper returns `(exifImageWidth, exifImageHeight, wire_orientation)` and bakes the orientation-nulling rule in:
- **Raw dims present**: returns `metadata.raw_width/raw_height` and `wire_orientation` is `metadata.orientation` as a string. Mobile re-derives display dims locally from the pair.
- **Drift-cohort fallback** (`raw_width/raw_height` are NULL): returns `asset.width/asset.height` (already display-space for that cohort) and `wire_orientation = None`. Feeding mobile display-space dims plus a non-null portrait orientation would make it re-apply the 5–8 swap and derive landscape dims for a portrait shot — the same double-rotation hazard the deleted `wire_orientation` helper was guarding. The fallback intentionally degrades to the old wire contract (display dims + nulled orientation) for drift rows.

Emit all three tuple elements verbatim on the response. Do **not** re-derive orientation at the call site or skip the helper — bypassing it reintroduces the double-rotation bug. Do **not** swap dims yourself in the adapter — that was a workaround for the old contract where the Gumnut API stored raw dims on `asset.width/height`, removed when the Gumnut API switched to storing display-space dims at ingest. Reintroducing it would double-correct against the new ingest semantics and stretch every portrait shot.

**Zero means unknown — coerce at every top-level `width/height` emit site.** the Gumnut API stores `0` (not `NULL`) for unknown dims on assets it couldn't probe, notably videos without EXIF width/height tags. The Immich mobile asset viewer (`asset_page.widget.dart::_getImageHeight`) divides `RemoteAssetEntity.width / height` to size its viewport and only guards against `null`; `0 / 0` yields `NaN` BoxConstraints and crashes the viewer on tap. `RemoteAssetEntity.width/height` is sourced from the **top-level** `SyncAssetV1.width/height` row (and `AssetResponseDto.width/height` on REST) — *not* the EXIF subobject. Every converter that emits a top-level `width`/`height` must coerce `0` to `None`: `asset.width if asset.width else None` (matching `build_asset_upload_ready_payload`, `convert_gumnut_asset_to_immich`, `gumnut_asset_to_sync_asset_v1`). The `exif_dims_and_orientation` helper bakes this rule in for the EXIF wire fields, but does **not** protect top-level row dims — those must apply the truthy guard explicitly at every emit site.

### Thumbnail variant selection by aspect ratio

`GET /api/assets/{id}/thumbnail?size=thumbnail` normally streams the 360px `thumbnail` variant, but `_retrieve_and_stream_variant` (`routers/api/assets.py`) upgrades it to the 720px `small` variant for **wide-landscape** assets — `width > height` AND aspect ratio above `_LANDSCAPE_SMALL_ASPECT_THRESHOLD` (see the constant for the value). The Immich web timeline is a justified-rows grid that renders every row at a fixed height; a 360px-longest-edge thumbnail of a landscape asset has a height of only `360/aspect`, so the wider the asset the shorter the tile and the more visibly it softens when upscaled to fill the row. Only assets past the threshold — where the upscale is visible — get bumped. The 720px `small` keeps those panorama/ultrawide cells crisp at roughly a quarter of the pixels of the 1440px `preview`, which is far more resolution than a timeline tile needs. Portrait assets are deliberately excluded — 360px lands on their height, which already meets the row height, so they stay crisp without the extra bandwidth. `small`/`preview`/`fullsize`/`original` requests and missing/zero dims pass through unchanged (the `width`/`height` `0`-means-unknown guard from *Asset dimensions and orientation* applies here too). Video upgrades resolve to `small_image` via the existing `_image`-suffix logic — so any variant the bump can target must be a member of both the `AssetVariant` type and `_VIDEO_IMAGE_VARIANTS`, or a video upgrade resolves to a bare (non-`_image`) key that isn't in `asset_urls` and 404s. The threshold is tunable.

The upgrade rewrites the variant **before** the `asset_urls` existence check, so it assumes the backend generates `small`/`small_image` whenever `thumbnail`/`thumbnail_image` exists — true today (image variants are CDN-resized URLs of the same uploaded file; a video's still-image variants materialize together). If that ever stops holding, a wide-landscape thumbnail request would 404 instead of degrading to the thumbnail. Gate the upgrade on the upgraded key's presence if that assumption breaks.

### Outbound asset checksums — emit base64 SHA-1, never the SHA-256

Immich's `checksum` field is a base64-encoded **SHA-1** (28 chars): clients compute the SHA-1 of a local file and compare it to this value for pre-upload dedup and for local↔remote asset linking ("merged" state) in the mobile client. Gumnut's `AssetResponse` carries two checksums — `checksum` (base64 **SHA-256**, 44 chars, always present) and `checksum_sha1` (base64 SHA-1, the Immich-facing value; nullable on older rows). Emitting the SHA-256 on the wire is a format mismatch that can never equal the client-computed SHA-1, so it silently breaks dedup and makes a backed-up photo show up as two timeline entries.

Use `resolve_immich_checksum(gumnut_asset)` from `routers/utils/asset_conversion.py` at **every** site that populates an outbound `checksum` field (`AssetResponseDto`, `SyncAssetV1`, the WebSocket `AssetUploadReadyV1Payload`). It returns `checksum_sha1`, or — when that is NULL — logs a WARNING with the `asset_id` and returns `""`. Never substitute `gumnut_asset.checksum` (SHA-256) or a literal like `"placeholder-checksum"`: a wrong-format value looks valid but never matches, whereas `""` produces a clean dedup no-match (the documented Immich behavior). The inbound dedup path is symmetric — `routers/api/assets.py::bulk_upload_check` keys on `checksum_sha1` and excludes rows without it.

### Stub endpoints — fail closed on auth/authz checks

The adapter has many stub endpoints (PIN code, session lock/unlock, change-password, etc.) that intentionally return success without doing real work, because Immich clients call them and expect a 2xx but the adapter doesn't model the underlying feature. That pattern is fine for purely informational stubs, but **don't apply it to endpoints whose contract is "tell the caller whether the request is authenticated/authorized"**. The Immich client trusts those answers — `auth_guard.dart` calls `/api/auth/validateToken` on app launch and lets the user past the login gate when the response is `authStatus=true`. A stub that always returns `True` lets unauthenticated clients past the gate, and the missing-auth failure only surfaces on the next API call (presenting as a sudden mid-session expiry rather than a missing-credential problem).

Rule of thumb: a stub may safely return success when it represents a feature the adapter doesn't implement. A stub that gates on auth must consult `request.state.jwt_token` (or the equivalent middleware-populated state) and return 401 when it's absent. Use `Depends(get_authenticated_gumnut_client)` if you also need an SDK client; do an inline `getattr(request.state, "jwt_token", None)` + `HTTPException(status.HTTP_401_UNAUTHORIZED, ...)` if you don't (mirroring `routers/api/auth.py::validate_access_token`).

### Restrictive filters the backend can't honor — short-circuit, don't drop

When an Immich endpoint accepts query filters that Gumnut doesn't model (e.g., `isFavorite`, `isArchived`, partner-shared assets), silently dropping the filter and returning unfiltered results is a wrong answer — the client asked to *restrict* results and got everything instead. For filters with that semantic, short-circuit to `[]` when the restrictive value is set:

```python
if isFavorite is True or isArchived is True:
    return []
```

Use `is True` rather than truthiness so `False` / `None` (which mean "no restriction") still return normal results — only the explicit `True` value asked for filtering. See `routers/api/timeline.py::get_time_buckets` and `routers/api/map.py::get_map_markers` for the established pattern.

This applies only to *restrictive* filters. Filters that ask for a broader result set (e.g., `withPartners=True` saying "also include partner-shared assets") can safely be dropped — the unfiltered result is a superset, not a wrong answer. Document in the docstring which filters are dropped and which short-circuit.

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

### Omit vs explicit-null in update-style DTOs — use `model_fields_set`

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

### Forwarding pagination parameters

When forwarding pagination params (`size`, `page`, `limit`) from an Immich request to a Gumnut SDK call, forward only what the client provided — don't substitute an adapter-side default. The SDK uses an `Omit` sentinel; just leave the kwarg out of the call so the Gumnut API applies its own default:

```python
from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE

search_kwargs: dict[str, Any] = {"query": request.description, ...}
if request.size is not None:
    # Clamp at the Gumnut API per-page ceiling.
    search_kwargs["limit"] = min(int(request.size), GUMNUT_API_MAX_PAGE_SIZE)
if request.page is not None:
    search_kwargs["page"] = int(request.page)
gumnut_results = await client.search.search(**search_kwargs)
```

Substituting an adapter-side default (e.g., `limit = int(request.size) if request.size else 50`) fragments the source of truth — the Gumnut API's `DEFAULT_PAGE_SIZE = 20` and an adapter-hardcoded 50 silently disagree, and a future change to the Gumnut API's default won't propagate. Same principle for any optional kwarg passed through the adapter: preserve the optionality, don't normalize.

Generated Immich DTO constraints can exceed the backend's per-page cap — e.g., `MetadataSearchDto.size` allows `le=1000.0` while the Gumnut API enforces `GUMNUT_API_MAX_PAGE_SIZE`, and the Immich mobile client uses these high values by default. Clamp at the adapter site against `GUMNUT_API_MAX_PAGE_SIZE` (defined in `routers/api/constants.py`); without it the Gumnut API 422s and the user sees a generic "Failed to ..." surface. **Don't shortcut by tightening the generated DTO** (e.g., dropping `Field(le=1000.0)` to `le=200.0`) — `routers/immich_models.py` is overwritten on every Immich version bump, which restores upstream's constraint and silently reintroduces the bug.

### Mobile-client null-aware string parsing

The Immich mobile app (Dart) parses some response fields with the null-aware `?.` operator — for example, `response.assets.nextPage?.toInt()` in the search service. Dart's `?.` short-circuits **only on `null`**, not on empty string. Returning `""` instead of `None` for an `Optional[str]` field whose mobile-side parser is `?.toInt()` / `?.toDouble()` crashes the client with `FormatException` on every successful response. Use `None` as the sentinel for any optional string the mobile client may parse numerically.

Concrete example: `SearchResponseDto.assets.nextPage` is typed `str | None` in the generated model; the adapter previously emitted `""`, which made every successful `/api/search/metadata` and `/api/search/smart` response crash the Android client. Audit any `Optional[str]` response field whose upstream Dart usage pattern is `?.<numeric-parse>()`.

### Counts and Aggregates

When a response only needs a count over a person's / album's assets, read the precomputed field off the parent entity rather than enumerating a paginator. `PersonResponse.asset_count` and `AlbumResponse.asset_count` are computed in O(1) by the Gumnut API and already trusted elsewhere in the adapter (e.g., `_immich_people_sort_key`, album conversion). Enumerating with `len([a async for a in client.assets.list(person_id=...)])` fans out into N paginated GETs of full asset payloads — this scaled to >10s on large persons.

Note that an `async for` paginator is always truthy: `if not client.assets.list(...)` is dead code, not an empty-list guard. The page contents are only known after iteration runs, so use the precomputed count rather than trying to short-circuit.

The SDK's `limit` kwarg on paginated methods (e.g., `client.assets.list(..., limit=20)`) is the **per-page** size, not a result cap. `async for` walks every page until `has_more` is false, so the loop will yield far more than `limit` items if the result set is larger. When you genuinely only want N items (e.g., a thumbnail preview, or a "non-empty" probe), break out explicitly:

```python
assets: list[AssetResponse] = []
async for asset in client.assets.list(local_datetime_after=..., limit=N):
    assets.append(asset)
    if len(assets) >= N:
        break
```

Without the break, a `limit=1` "is this non-empty?" probe on a busy day burns one round-trip per matching asset.

### Parallel Fan-Out with `asyncio.gather`

For endpoints that fan out N parallel backend calls where partial results are friendlier than a 500 (e.g., the OnThisDay memories carousel — N-1 years still produces a useful response), pass `return_exceptions=True` so a single transient failure doesn't cancel the others. Filter on `Exception`, not `BaseException`, so `asyncio.CancelledError` (which inherits from `BaseException`) propagates instead of being swallowed as a backend error:

```python
results = await asyncio.gather(
    *(_per_year(client, y) for y in years),
    return_exceptions=True,
)
for year, result in zip(years, results):
    if isinstance(result, Exception):
        logger.warning(f"...failed for {year}", exc_info=result)
        # substitute a degraded value
    elif isinstance(result, BaseException):
        # Re-raise CancelledError and other control-flow signals so request
        # cancellation isn't silently swallowed.
        raise result
    else:
        ...
```

`gather(return_exceptions=True)` captures `CancelledError` like any other exception, so a naive `isinstance(result, BaseException)` check disguises cancellation as a transient failure. See `routers/api/memories.py::_gather_year_assets` for the canonical shape.

#### Bounded fan-out for per-item SDK calls

For bulk endpoints that have to call a single-item SDK method per input (no bulk SDK variant exists — e.g., `client.people.update`, `client.people.delete`, or per-album SDK calls inside a multi-album fan-out), use `gather_with_concurrency` from `routers/utils/concurrency.py` instead of a sequential `for` loop. It runs coroutines in parallel under a `BULK_FANOUT_CONCURRENCY_LIMIT` semaphore, preserves input order in the result list, and propagates the first exception (cancelling siblings). The same helper applies when the parallelizable unit is a multi-step coroutine rather than a single SDK call (e.g. `reassign_faces` parallelizes per-`(asset, sourcePerson)` pairs whose dominant cost is a `client.faces.list` call; the inner per-face `client.faces.update` loop stays sequential because pairs almost always yield 0–1 faces).

```python
from routers.utils.concurrency import gather_with_concurrency

results = await gather_with_concurrency(
    [_update_one_person(client, item) for item in people_data.people]
)
```

When the endpoint returns `List[BulkIdResponseDto]`, catch per-item errors **inside** the per-item coroutine and return a typed result — don't rely on the helper to surface them, since `asyncio.gather` (default) cancels pending siblings on the first exception. When the endpoint contract is "abort the batch on first error" (e.g. `delete_people` returning 204), the default propagation is exactly right; let the global `GumnutError` handler take over.

For the error-classification half of the per-item coroutine, use `classify_bulk_item_call` from `routers/utils/bulk.py` instead of re-rolling the `APIStatusError` / `GumnutError` try/except. It mirrors the per-chunk policy in `chunked_per_item_bulk` (`classify_bulk_item_error` for `APIStatusError`, `log_bulk_transport_error` + `unknown` for transport failures) and returns `None` on success or a classified enum value (`Error1` / `BulkIdErrorReason`). Wrap the entire SDK-touching segment in one call — including any helper that itself issues SDK calls (e.g. `_resolve_thumbnail_face_id`'s `client.faces.list`) — so the helper catches errors from every SDK round-trip on the path. Endpoint-specific non-SDK exceptions (UUID parse `ValueError`, `HTTPException` from a logical 4xx branch) stay at the call site:

```python
sdk_error = await classify_bulk_item_call(
    _do_one_item(client, item),
    error_enum=Error1,
    log_context="update_people",
    log_extra={"person_id": item.id},
)
return BulkIdResponseDto(id=item.id, success=sdk_error is None, error=sdk_error)
```

See `routers/api/people.py::_update_one_person` for the canonical multi-step shape (UUID parse → SDK call wrapped → HTTPException out) and `routers/api/albums.py::_add_assets_to_one_album` for the single-call shape. The `tests/unit/utils/test_bulk.py::TestClassifyBulkItemCall` suite pins the helper's contract.

Pin the contract with a concurrency-counter test: an `asyncio.Lock`-guarded `active` / `peak` counter inside the per-item side_effect, asserting `peak > 1` (parallel) and `peak <= BULK_FANOUT_CONCURRENCY_LIMIT` (bounded). See `tests/unit/utils/test_concurrency.py::test_caps_concurrent_in_flight_calls` and the per-endpoint variants in `tests/unit/api/test_people.py` / `test_albums.py`.

If you write a *new* fan-out helper instead of using `gather_with_concurrency`, watch for unawaited-coroutine leaks on cancellation: when callers pass eagerly-constructed coroutines (`[some_coro(x) for x in xs]`) and your wrapper task awaits something *before* `await coro` (a semaphore acquire, a queue, etc.), the first exception in any sibling makes `asyncio.gather` cancel waiting wrappers — the inner `coro` is never awaited and is GC'd later as `RuntimeWarning: coroutine was never awaited` (noisy precisely on the error path). Either build the inner coroutine lazily inside the wrapper, or `coro.close()` it explicitly when the pre-`await coro` cancellation hits. See `gather_with_concurrency`'s `_run` for the canonical shape and `tests/unit/utils/test_concurrency.py::test_cancellation_does_not_warn_unawaited_coroutines` for the regression test pattern.

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

**Cross-chunk atomicity is not guaranteed.** Backend bulk endpoints (and the SDK methods that wrap them) commit each call atomically — a single chunk either fully commits or writes nothing — but that guarantee does not extend across the chunked loop above. A failure on chunk N (N ≥ 2) leaves chunks 1..N-1 already committed, with no compensating rollback and no per-chunk error report. The exception propagates as one 5xx to the client. Document this in the handler docstring when the SDK markets the underlying endpoint as atomic (e.g., `bulk_update_assets`), so future readers don't assume the guarantee transitively holds through the adapter's chunking layer.

Pin the no-swallow contract with a `test_*_propagates_sdk_error` test per bulk flow — mock the bulk call to raise via `make_sdk_status_error(500, ...)` and assert `pytest.raises(APIStatusError)`. Without this test, a future refactor that wraps the bulk call in `try/except` would silently regress the contract. See `tests/unit/api/test_assets.py::TestDeleteAssets::test_delete_assets_force_false_propagates_sdk_error` for the canonical shape.

**Reads that feed a bulk write must use `state="all"`.** `client.assets.list(ids=...)` defaults to the live-only filter, so trashed (soft-deleted) ids are silently absent from `page.data`. When a "bulk GET + bulk PATCH" flow reads current values to compute a per-asset write-back (e.g. `update_assets`' `dateTimeRelative` / standalone-`timeZone` modes), pass `state="all"` — otherwise the read-driven path silently skips assets that an unconditional bulk write (one that forwards every id regardless of trash state) would have updated, an asymmetry the same request can expose across different fields. Mirrors sync hydration's read (see `routers/api/sync/entity_fetch.py`). Pin it with a test asserting the read kwargs include `state="all"`.

**Per-item response contract variant.** Some Immich bulk endpoints (e.g. `PUT`/`DELETE /api/albums/{id}/assets`) must return `List[BulkIdResponseDto]` with per-id `success` / `error` mapping, so the no-swallow contract above does not apply — the handler has to catch upstream errors locally and translate them into per-id `Error1` values. Use `chunked_per_item_bulk` from `routers/utils/bulk.py`: it owns the chunking loop and the `APIStatusError`/`GumnutError` mapping (errors are classified via `classify_bulk_item_error` and transport failures are logged with `chunk_size` + `request_size` extras), and yields per-chunk outcomes as `BulkChunkOutcome[T]` with either a `response` or an `error`. Callers compose the final per-asset list — that's where response-shape variation lives (e.g. `add` accumulates `added`/`duplicate`/`not_found` sets and walks input order to look up each id; `remove` only needs an error vs success branch). See `routers/api/albums.py::add_assets_to_album` / `remove_asset_from_album` for canonical call sites and `tests/unit/utils/test_bulk.py` for the helper's contract.

Pin the chunking math with exact-boundary tests at `total = BULK_CHUNK_SIZE` (one chunk, no split) and `total = BULK_CHUNK_SIZE + 1` (two chunks, second is a single element) — these catch off-by-one regressions a future hand-rolled `if len(ids) > N` split would introduce. See the parametrized cases in `tests/unit/utils/test_bulk.py::test_splits_oversized_input_into_ordered_chunks` and `tests/unit/api/test_albums.py::test_*_chunks_large_request`.

When the SDK doesn't yet expose a typed method for a backend endpoint (Stainless regenerates on a delay after each backend release), call the raw HTTP layer directly via `AsyncGumnut.post()` / `.delete()` with `cast_to=type(None)` for 204-returning endpoints:

```python
await client.post("/api/assets/trash", body={"ids": gumnut_ids}, cast_to=type(None))
await client.delete("/api/assets", body={"ids": gumnut_ids}, cast_to=type(None))
```

`AsyncGumnut` extends `AsyncAPIClient`, whose `.post()` / `.delete()` methods are public, route through the same JWT auth, retry, and response-hook plumbing as the typed methods, and surface the same `GumnutError` hierarchy. Don't import from `gumnut._types` — `cast_to=type(None)` works without it.

### WebSocket Emission

`emit_user_event` and `emit_session_event` (in `services/websockets.py`) are **fire-and-forget**: they catch `SocketIOError` from the underlying transport, log at WARN with `exc_info=True`, and return normally. **Do not wrap call sites in `try/except SocketIOError`** — the central swallow is the contract, and per-site catches are duplication. If the surrounding block needs to handle other exception types (e.g., DTO conversion before the emit, like `_emit_upload_events` in `routers/api/assets.py`), the broader try/except can stay; just don't add a separate `except SocketIOError` branch.

For chunks that fire one event per id (e.g. `ASSET_DELETE`'s single-id wire shape), use `emit_user_event_per_id(event, user_id, payload_ids)` instead of rolling an inline `asyncio.gather(*(emit_user_event(...) for ... in chunk))` — the helper centralizes the per-id gather wave so callers don't duplicate it. Pass a generator or list of pre-stringified ids; the helper consumes the iterable once.

**Bulk write endpoints whose SDK call returns no per-asset payload.** Some bulk writes (e.g., `bulk_update_assets`) return an empty body, so the adapter doesn't have the updated asset DTO needed to mirror the single-asset path's `on_asset_update` payload. The default is to **skip WebSocket emission** from the bulk path rather than re-fetch via `list_assets(ids=[...])` — the extra round-trip per chunk isn't justified when mobile triggers a generic sync refresh on its own and web has optimistic UI for these flows. Document the trade-off explicitly in the handler docstring so future readers don't reintroduce the round-trip on a hunch. Re-fetching is the right call only when a concrete client surface stays visibly stale until next sync; gate that decision on observed behavior, not theory.

When a change modifies **when** an existing WebSocket event fires (deferral, debounce, batching, conditional skip) — not just when adding a brand-new event — the trigger described in `docs/references/websocket-events-reference.md` and the Notes column in `docs/architecture/websocket-implementation.md`'s Phase 1 table go stale. Update both docs and bump their `last-updated` whenever the emit-timing contract changes, even if the event itself already existed. The "Implementing New Endpoints" checklist step 9 covers new emit sites; this rule covers timing changes to existing ones. The image-vs-video `on_upload_success` deferral is the canonical example — the docs had previously claimed "the Gumnut API thumbnails are synchronous", which became false the moment videos started waiting.

### Immich Client Error Handling

- **Observed behavior:** Immich mobile and web clients have no HTTP 429 (rate limit) handling. A 429 causes sync failures, broken thumbnails, and upload errors with no automatic recovery.
- **Adapter contract:**
  - Never forward 429 responses from the Gumnut API to Immich clients.
  - The Gumnut SDK (Stainless-generated) has built-in retry for 429, 5xx, and connection errors with exponential backoff, ±25% jitter, and `Retry-After` header support (see [SDK retry docs](https://www.stainless.com/docs/sdks/configure/client/#retries)). Configure `max_retries` on the client — **do not add a custom retry wrapper** on top, as it will stack with SDK retry and cause retry amplification.
  - The global `GumnutError` handler catches `RateLimitError` explicitly and returns 502 (not 429) to Immich clients. `map_gumnut_error` does the same when called directly from upload paths.

## Testing

- All tests should be async and use `@pytest.mark.anyio` decorator
- Run tests from the project directory, not repository root
- Use model factories for test data creation
- Avoid asserting on logging in tests by default — logging is usually non-functional behavior. Exception: when log level itself is an explicit contract (for example, upstream status severity policy), assertions may verify level/metadata while avoiding brittle full-message matching.
- When mocking SDK paginator calls used with `async for` (e.g., `client.faces.list`), use `Mock(return_value=MockSyncCursorPage([...]))` — not `AsyncMock`. `AsyncMock` wraps the return in a coroutine, which breaks `async for` iteration. Use `AsyncMock` only for calls consumed with `await`.
- The shared `MockSyncCursorPage` (`tests/conftest.py`) yields a flat list — it can't distinguish "limit is per-page" from "limit is a result cap" semantics. When testing code that explicitly `break`s out of `async for` after N items (e.g., `_fetch_assets_for_day`), build a small paginating mock that tracks page boundaries so a regression that drops the break visibly walks extra pages. See `tests/unit/api/test_memories.py::_PaginatedListing` for the canonical shape.
- When mocking SDK response objects whose attributes are checked for truthiness (e.g., `if asset.metadata:`), explicitly set the attribute to its expected falsy value (`mock.metadata = None`). Unset Mock attributes return a truthy `Mock` object, silently flipping the branch and producing confusing downstream errors instead of clean `None`-path coverage. Audit `Mock`-based fixtures whenever an SDK field is renamed or added — grep across `tests/` for every `Mock()` construction of the relevant entity, including shared fixtures in `tests/conftest.py` and `tests/unit/api/sync/conftest.py` AND per-file inline mocks. Missing one is enough to silently flip a downstream Pydantic validation result.
- The same audit applies to any code change that introduces a **new branching predicate** on an SDK attribute that previously didn't matter (e.g., adding `if asset.mime_type.startswith("video/"):` to a helper). Unset Mock attributes will satisfy `.startswith(...)` (returns a truthy `Mock`), `==` (matches another `Mock`), and most other predicates — silently making every existing fixture take the new branch. Helper-based fixtures (e.g., `_make_mock_asset_with_urls`) should grow an explicit kwarg for the newly-branched attribute (with a realistic default that matches the fixture's stated shape), and existing call sites whose `asset_urls` describe video MIME should pass `mime_type="video/mp4"` to pin the realistic call path. Without this, a future refactor that flips a constant (e.g., adding `"original"` to a "video-only variants" set) can silently break video playback without any test failing.
- The same audit also applies when a converter starts **passing a new SDK field straight into a Pydantic DTO** (e.g., wiring `AssetResponse.thumbhash` into `AssetResponseDto`/`SyncAssetV1`), even with no branch involved. Here the failure mode is louder than a flipped branch — an unset `Mock` attribute is neither `str` nor `None`, so it raises `ValidationError` on every `Mock`-based asset that flows through the converter — but it surfaces only at full-suite runtime, not in the converter's own unit test. A task spec may name only the converter-local fixtures (`tests/conftest.py`); the real blast radius is every per-file inline asset mock too (e.g., `tests/unit/api/test_assets.py`, `test_memories.py`, `test_search.py`, and `tests/unit/api/sync/`). Grep `tests/` for every `Mock()`/`checksum_sha1` asset-construction site and set the new field (typically `= None`) before relying on the converter; don't trust a spec's fixture list to be complete.
- Tests that coordinate concurrent tasks/requests with `asyncio.Event` waits must bound the coordination with `asyncio.timeout(...)` — the repo has no pytest-timeout configured, so a regression in the ordering assumptions deadlocks the whole suite instead of failing one test. See `tests/unit/utils/test_token_refresh.py` for the pattern.
- Do not add `__init__.py` to test directories — the project uses pytest's rootdir-based import resolution. Adding `__init__.py` switches pytest to package-based imports, breaking test discovery.
- When code under test sleeps on a module-level delay/timeout constant (e.g., `_VIDEO_EMIT_DELAY_SECONDS` in `routers/api/assets.py`), **every** test that exercises the delayed path must patch the constant to a near-zero value (typically `0.0`) — not just the test that explicitly asserts on the deferral. Tests that only touch the delayed path incidentally (drains, end-of-test cleanup, error-path coverage) silently wait the real delay otherwise, ballooning suite runtime. Use `patch("module.path._CONSTANT_NAME", 0.0)` inside the same `with` block as the other mocks so the patch covers the spawned task's lifetime. When adding a new delay constant, grep tests for every call site that reaches it and audit each test for an explicit patch — `tests/unit/api/test_assets.py::test_upload_regular_video_proceeds` and `test_video_upload_defers_websocket_events` are the canonical pattern.

## Logging

Use structured logging with key/value metadata in the `extra` dict. Include relevant identifiers for traceability.

```python
logger.info(f"Created library {library.id}", extra={"library_id": library.id})
logger.info("WebSocket connected", extra={"sid": sid, "user_id": user_id, "device_type": session.device_type})
```

This enables better searching and correlation in Sentry.

### Upstream response log levels

For responses/errors from upstream Gumnut API calls, use status-based severity:

- `404` → `INFO`
- Other `4xx` (including `400`, `401`, `403`, `422`, `429`) → `WARNING`
- `5xx` → `ERROR`

When possible, use shared helpers in `routers/utils/error_mapping.py` (`upstream_status_log_level` / `log_upstream_response`) instead of ad-hoc `if/else` logging branches.

**Reserved `extra` keys**: Python's `LogRecord` has reserved attributes (`filename`, `module`, `name`, `msg`, `args`, `levelname`, `pathname`, `lineno`, etc.). Using these as `extra` keys causes a `KeyError` at runtime. Use prefixed names instead (e.g., `upload_filename` instead of `filename`).

## Pull Requests

- When updating pull requests with additional commits, update the PR description to include the latest changes
- Always run tests and formatting before creating a PR
