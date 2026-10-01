---
title: "Asset Field Conversion"
last-updated: 2026-10-01
---

# Asset Field Conversion

## Favorite and rating are one dial

The adapter exposes the Gumnut API's resolved metadata rating through Immich's two fields: `isFavorite` is true exactly when `metadata.rating == 5`, while EXIF/sync `rating` emits 1–5 and normalizes 0, null, or an out-of-range legacy value to null. Every asset read feeding `AssetResponseDto`, `SyncAssetV1`/`V2`, or the upload-ready WebSocket payload must request `include=metadata` and derive the heart through `is_asset_favorite`; hardcoded false values recreate the silent-revert bug after the next sync.

Writes land on the same USER-layer dial. `isFavorite: true/false` writes 5/0. An explicitly-present `rating` writes 0–5 and wins when both fields are present. Immich uses explicit `rating: null` for unrated, so the adapter maps it to USER 0; forwarding backend null would instead clear the USER override and reveal an embedded FILE-layer rating. Values outside 0–5 return 422.

When replaying rating changes to already-checkpointed clients, emit both `ASSET_UPDATED` and `METADATA_UPDATED` events without rewriting current ratings. Rewriting would turn a FILE-derived value into a persistent USER override.

## Derive multi-path fields through one helper

A field the adapter surfaces from more than one path must be derived identically in each: the write handler, the paired read handler, **and** the sync-stream converter (`routers/api/sync/converters.py`). Route any value mapped from a Gumnut field through a shared helper instead of hardcoding it per site, or the same entity reads back differently by path — use `resolve_asset_location` for asset city/country/coordinates, for example. When you touch one emit site for such a field, grep for every constructor of the Immich response type (`AssetFaceResponseDto`, `SyncAssetFaceV1` / `SyncAssetFaceV2`, …) before assuming it has one home.

## Reading Gumnut asset fields — request them via `include`

The Gumnut API returns a **lean default** asset response behind a JSON:API-style `?include=` parameter: an omitted `include` returns only the lean core and none of the heavy fields, which is why `faces`, `people`, and the `file_data` scalars (`device_asset_id` / `device_id` / `file_created_at` / `file_modified_at` / `checksum` / `file_size_bytes`) are nullable. So **any** `client.assets.list` / `client.search.search` / `client.assets.retrieve` whose result feeds a conversion that reads `metadata`, `people`, or a `file_data` scalar must pass the matching `include` — otherwise those fields arrive `null` and the Immich asset is silently corrupted (empty checksum, null size/EXIF, missing people). Mock-based unit tests don't catch a missing token.

Use the constants in `routers/utils/asset_conversion.py`, chosen by what the conversion **downstream of the call** actually reads:

| Constant | Tokens | Use for |
|----------|--------|---------|
| `ASSET_INCLUDE` | `metadata, people, file_data` | Reads feeding `convert_gumnut_asset_to_immich` (it emits `people`): `get_asset_info`, search, memories, the upload-success retrieve. |
| `ASSET_INCLUDE_NO_PEOPLE` | `metadata, file_data` | The sync-stream `entity_fetch` reads, whose converters read the `file_data` scalars but never `people`. |
| `ASSET_INCLUDE_METADATA_ONLY` | `metadata` | Reads that touch only `metadata`: timeline buckets (place names and GPS), map markers (GPS), the bulk per-asset datetime rewrite (`original_datetime`). |

Reads that consume only **lean-core** fields (`id`, `mime_type`, `width`/`height`, `duration`, `trashed_at`, `local_datetime`, `stack_id`, `kind`, `file_size_bytes`) request **no** `include` — those stay populated regardless (today: trash-id collection, asset-count stats, the `/faces` width/height read, and `/download/info` sizes). `stack_id` earns its place on that list: the timeline's burst collapse still depends on it arriving as a lean-core field alongside an `include=metadata` request, and if it ever moved behind a token the feature would degrade to no collapse and no badges with no error and no failing test. `asset_urls` is the exception when a call may stream non-thumbnail bytes: `_retrieve_and_stream_variant` now passes `include=variants`, because it serves the `small`/`preview`/`fullsize`/`original` rungs (and the video `_image` equivalents) and even a `thumbnail` request can aspect-upgrade to `small`. `faces` is never requested off the asset — the adapter reads faces from the dedicated `/faces` endpoint. The `create()` (buffered upload) and `update_asset()` (PATCH) responses keep the full shape and expose no `include` param, so they need no change.

Bumping `gumnut-sdk` can itself relax a previously-non-null asset field to `| None` as part of this migration (e.g. `file_modified_at` went `datetime` → `datetime | None`), which then needs a null-guard at every read site (`resolve_file_modified_at` falls back through `metadata.modified_datetime → file_data.file_modified_at → capture time` so the required Immich `fileModifiedAt` is never null). Run `uv run pyright` after any `gumnut-sdk` bump to surface newly-required guards.

**But pyright does not cover a bump that renames or removes a parameter.** Several call sites build a `dict[str, Any]` and splat it (`client.search.search(**search_kwargs)`, the `assets.list` / `assets.counts` / `events.get` sites); `dict[str, Any]` erases the keys, so a dropped parameter type-checks clean and raises `TypeError: … got an unexpected keyword argument …` only when the endpoint is actually called. The 0.131→0.137 bump hit exactly this — `captured_after`/`captured_before` became `local_datetime_after`/`local_datetime_before` and pyright reported 0 errors on the stale call. So after a bump, also introspect the new signatures against every splatted call site rather than trusting a green type check:

```
uv run python -c "import inspect; from gumnut.resources.search import AsyncSearchResource; print(list(inspect.signature(AsyncSearchResource.search).parameters))"
```

**Read the file/provenance scalars from the nested `file_data` object, not the deprecated flat top-level fields.** The Gumnut API exposes the group in two shapes under the same `include=file_data` gate: the preferred nested `gumnut_asset.file_data` object (`checksum_sha1`, `file_modified_at`, `file_size_bytes`, `device_asset_id`, `device_id`, `file_created_at`, `checksum`) and the equivalent flat top-level scalars (`gumnut_asset.checksum_sha1`, …). Top-level `file_size_bytes` is not one of them: see [Edited state](asset-edits-and-downloads.md#edited-state-and-exact-original-downloads). The flat scalars are deprecated and being removed, so read `gumnut_asset.file_data.<field>` guarding `file_data is None` — `file_data` is `None` when `include=file_data` isn't requested, and that guard preserves the legacy fallbacks (empty Immich checksum on a null `checksum_sha1`, null size, the modify-time cascade above). The whole group is gated by the `file_data` include token either way.

At **runtime**, a field the server omits — an older Gumnut API during a rollout, or a field gated behind an `include` the call didn't request — does **not** raise `AttributeError` on access. The SDK builds responses with non-validating construction (`construct_type`, since `_strict_response_validation` is off), which materializes every field the model declares and defaults an omitted one to `None`. So read such a field with plain attribute access and treat `None` as "absent"; a `getattr(obj, "field", default)` guard written to catch an `AttributeError` fallback is dead code (the attribute is always present), and a docstring claiming the access can raise misleads the next reader. `AttributeError` is reachable only when the *pinned* SDK model doesn't declare the field at all — which the version pin precludes — so verify the installed model actually exposes a field (e.g. `grep` the installed `gumnut/types/*.py`) rather than trusting a version number, since a Stainless commit's internal `version` string can differ from the published release that first ships the field. The exception is a field the Gumnut API serves before any published SDK types it: it arrives as an untyped extra, so `getattr(obj, "field", default)` is the correct read until the SDK declares it. The pinned SDK now declares `LibraryResponse.role`, so `services/library_resolver.py::first_owned_library_id` reads it directly; tests still build rows with `Model.construct(...)`, the path responses take.

## Asset dimensions and orientation

Emit known top-level `asset.width` / `asset.height` as display-space
dimensions; never swap them in the adapter. For EXIF dimensions and orientation,
use `routers/utils/asset_conversion.py::exif_dims_and_orientation` at every
emit site and emit all three returned values unchanged. Its docstring owns the
raw-dimension versus drift-cohort fallback and double-rotation rationale.
Do not re-derive orientation at a call site.

**Zero means unknown — coerce at every top-level `width/height` emit site.** the Gumnut API stores `0` (not `NULL`) for unknown dims on assets it couldn't probe, notably videos without EXIF width/height tags. The Immich mobile asset viewer (`asset_page.widget.dart::_getImageHeight`) divides `RemoteAssetEntity.width / height` to size its viewport and only guards against `null`; `0 / 0` yields `NaN` BoxConstraints and crashes the viewer on tap. `RemoteAssetEntity.width/height` is sourced from the **top-level** `SyncAssetV1.width/height` row (and `AssetResponseDto.width/height` on REST) — *not* the EXIF subobject. Every converter that emits a top-level `width`/`height` must coerce `0` to `None`: `asset.width if asset.width else None` (matching `build_asset_upload_ready_payload`, `convert_gumnut_asset_to_immich`, `gumnut_asset_to_sync_asset_v1`). The `exif_dims_and_orientation` helper bakes this rule in for the EXIF wire fields, but does **not** protect top-level row dims — those must apply the truthy guard explicitly at every emit site.

## Outbound asset checksums — emit base64 SHA-1, never the SHA-256

Use `routers/utils/asset_conversion.py::resolve_immich_checksum` at every
outbound checksum site (`AssetResponseDto`, sync rows, and upload-ready payloads).
Request `include=file_data`; the helper reads `file_data.checksum_sha1` and
logs a warning before returning `""` when it is absent. Never substitute
SHA-256 or a placeholder. The helper's docstring owns the client dedup and
local/remote linking rationale. The inbound path is symmetric:
`routers/api/assets.py::bulk_upload_check` keys on `checksum_sha1` and excludes
rows without it.

## Asset date fields and DTO normalization

- **Asset date fields**: Any endpoint or converter that emits Immich asset date fields must use the shared helpers in `routers/utils/asset_conversion.py`:
  - `resolve_capture_datetime`, `resolve_file_created_at`, `resolve_local_date_time` — for capture-time fields. The Gumnut API resolves `asset.local_datetime` from `metadata.original_datetime → file_created_at → created_at` internally, so the helpers trust it as the single source of truth and the adapter must not re-add a fallback chain. The helpers then handle Immich's actual-UTC `fileCreatedAt` and keep-local-time `localDateTime` formats.
  - `resolve_file_modified_at` — for `fileModifiedAt`. Its cascade is `metadata.modified_datetime → file_data.file_modified_at → capture time`; its docstring owns the rationale. Do not "align" this with the capture-time helpers.
- **Immich DTO datetime fields are `AwareDatetime`; Gumnut datetimes can be naive.** The Gumnut API serializes a local capture datetime timezone-naive when the capture timezone is unknown, and a non-null naive value fails an `AwareDatetime` field's validator (unhandled `ValidationError` → 500). Don't assume a Gumnut datetime is tz-aware: route local-capture-time fields through `to_immich_local_datetime` (labels naive values UTC keep-local-time, passes `None` through). Server row timestamps (`created_at`/`updated_at`) are tz-aware and pass through raw. This applies beyond asset fields: `AlbumResponseDto.startDate`/`endDate` are the min/max of assets' `local_datetime`, so they use the same helper as each asset's `localDateTime`.
- **Immich DTO fields can constrain more tightly than the Gumnut value's own type** — the same unhandled-`ValidationError` → 500 trap as the naive-datetime bullet above, in another guise. Normalize before constructing the DTO. Example: `ExifResponseDto.rating` is `ge=1, le=5`, but cameras write 0 (XMP:Rating) or the deprecated -1 for "unrated"; `normalize_rating` bounds any value to 1-5 or None so an unrated/out-of-range rating never reaches the DTO.
