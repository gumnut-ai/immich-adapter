---
title: "Immich v2.7.5 → v3.0 API Change Analysis"
status: active
created: 2026-06-16
last-updated: 2026-07-02
---

# Immich v2.7.5 → v3.0 API Change Analysis

## Context

The immich-adapter currently targets the Immich **v2.7.5** release (pinned in
`.immich-container-tag`). Immich **3.0** has since shipped (**v3.0.0** GA,
2026-06-30). This document is a structural diff of the two OpenAPI specs — 2.7.5
against **v3.0.0-rc.0**, re-validated against the GA spec (see the GA validation
note below) — scoped to what the adapter must change to retarget 3.0.

Both specs are OpenAPI 3.0.0. The diff was produced by comparing
`immich/open-api/immich-openapi-specs.json` (2.7.5) against
`immichv3/open-api/immich-openapi-specs.json` (3.0.0-rc.0).

**Surface delta:**

| | v2.7.5 | v3.0.0-rc.0 | Δ |
|---|---|---|---|
| Paths | 163 | 173 | +17 added, −7 removed, 1 method dropped |
| Schemas | 361 | 373 | +34 added, −22 removed, 102 "changed" |

> ⚠️ The "102 changed schemas" figure is misleading. The large majority are
> **codegen-only noise** (see §1) that does not change the JSON on the wire. The
> real behavioral surface is much smaller and is captured in §2–§6.

> **GA validation (2026-07-02).** This analysis was produced against
> `v3.0.0-rc.0`. The GA spec (`v3.0.0`, tagged 2026-06-30) has since been diffed
> against `v3.0.0-rc.0`: **no material changes for the adapter.** No endpoints or
> schemas were added, removed, or restructured, and every §2–§6 item below is
> byte-stable between rc.0 and GA. The only spec differences are:
>
> - **UUID annotations** — `format: uuid` plus a strict UUID `pattern` added to
>   ~90 ID fields. Same codegen-noise class as §1; the wire bytes are unchanged.
> - **Datetime pattern relaxed** — the `date-time` regex now also accepts a
>   timezone offset (`Z` → `Z | ±HH:MM`) on every datetime field. Validation
>   annotation only; Immich still emits UTC `Z`.
> - **Admin config validation loosened** — cron / time-of-day `pattern`s dropped
>   from the system-config DTOs (`DatabaseBackupConfig`, integrity and
>   library-scan job configs). Admin-only, not adapter-facing.
> - **New optional header** — `x-immich-hls-pos` on the HLS playlist endpoint,
>   part of the already-new-in-3.0 adaptive-streaming surface (§4).
> - **Doc-string fixes** — face "created manually"; `AssetBulkUploadCheckItem` /
>   `Result.id` reframed as a client-supplied echo token (not an asset ID); and
>   `AssetBulkUpdateDto.dateTimeRelative` corrected from "seconds" to "minutes".
>   The Immich server has always applied `dateTimeRelative` as **minutes** (the
>   SQL adds `${delta} minute` in 2.7.5 and 3.0 alike), so the adapter's current
>   seconds-based handling in `routers/api/assets.py` is off by 60× — a
>   pre-existing bug the GA doc-string fix merely surfaced, independent of the
>   v3 retarget.
>
> The rc.0-based plan below therefore stands unchanged for GA.

### Reproducing the diff

The comparison scripts are ad hoc Python/jq over the two JSON specs (path set diff,
operation-signature diff, schema property diff). They are not checked in. The same
probes were re-run for the rc.0 → GA (`v3.0.0`) comparison behind the GA validation
note above. Key probes: path-set difference, per-operation
parameter/requestBody/response signature, and per-schema property/required/enum diff.

## Goals

1. Inventory every API change between 2.7.5 and 3.0.0-rc.0.
2. Separate codegen noise from real behavioral changes.
3. Map each behavioral change to the adapter code it affects.
4. Give a prioritized retarget plan.

---

## 1. Codegen noise — safe to ignore for wire compatibility

These dominate the schema diff but **do not change the JSON on the wire**. They
will, however, change the generated `routers/immich_models.py` symbols/types when
the spec is regenerated.

- **`allOf:[{$ref}]` → bare `$ref`** for enum-typed fields (~50 occurrences:
  `type`, `role`, `visibility`, `order`, `status`, `level`, `command`, `name`,
  `action`, `axis`, `colorspace`, `format`, …). Pure generator change in how
  nullable enum refs are emitted.
- **`number` → `integer`** on dozens of fields (`rating`, `page`, `size`,
  `height`, `width`, exif dimensions, `iso`, `port`, `interval`, `timeout`, …).
  Semantically already integers; tightens validation, no wire change. Flips
  generated Python types `float → int`.
- **Format annotations added**: `string → string(uuid|email)`,
  `string(uri) → string`, `integer(int64) → integer` (now carrying min/max
  bounds). Validation/codegen only.
- **`APIKey*` → `ApiKey*`** schema rename (4 schemas: `ApiKeyResponseDto`,
  `ApiKeyCreateDto`, `ApiKeyCreateResponseDto`, `ApiKeyUpdateDto`). Casing only —
  renames generated symbols, identical wire shape.

**Action:** when regenerating models, expect a large but mechanical churn. Do not
mistake it for behavioral change.

---

## 2. Breaking wire-format changes (highest impact)

| Change | Detail | Adapter impact |
|---|---|---|
| **`duration` string → integer ms** | `AssetResponseDto`, `TimeBucketAssetResponseDto`, `AssetMediaCreateDto`. Was `"HH:MM:SS.ffffff"` interval string, now integer **milliseconds**, nullable on `AssetResponseDto`. | `routers/utils/asset_conversion.py:format_duration` emits the interval string today — must emit int ms. Touches `routers/models.py:69`, and the `duration` fields in `routers/immich_models.py`. Affects every asset/timeline/upload response. |
| **`AlbumResponseDto` restructured** | Removed `assets`, `owner`, `ownerId`. Owner now derived from **`albumUsers[0]`** (now `minItems:1`; documented ordering: owner first, then auth user if different, rest alphabetical). `shared` now required. | Album responses no longer inline assets or owner. Album conversion must populate `albumUsers[0]` as owner and stop emitting `owner`/`ownerId`/`assets`. |
| **`AssetResponseDto` face/device fields** | Removed `deviceAssetId`, `deviceId`, `unassignedFaces`. `people`: `PersonWithFacesResponseDto` → `PersonResponseDto` (no inline face bounding boxes). | No inline face geometry on assets. Schemas `PersonWithFacesResponseDto` and `AssetFaceWithoutPersonResponseDto` deleted. |
| **Shared-link tokens removed** | `SharedLinkResponseDto.token` gone; `GET /shared-links/me` lost `password`/`token` query params; `SharedLinkEditDto.changeExpiryTime` gone; `PUT /albums/{id}/assets`, `PUT /shared-links/{id}/assets`, `PUT /albums/assets` lost `key`/`slug` params. | Shared-link / anonymous (key/slug) access model changed — `routers/api/shared_links.py` and key/slug access path need rework. |
| **`deviceId`/`deviceAssetId` dropped from search/upload DTOs** | `MetadataSearchDto`, `SmartSearchDto`, `RandomSearchDto`, `StatisticsSearchDto`, `AssetMediaCreateDto`. | Device-based dedup/filtering removed across search + upload. |
| **Asset replace removed** | `AssetMediaStatus` lost `replaced`; `Permission` lost `asset.replace`; `AssetMediaReplaceDto` deleted. See also removed `PUT /assets/{id}/original` in §3. | |
| **`LicenseResponseDto`** | Lost `activatedAt`, `activationKey`, `licenseKey`. | Licensing response shape changed. |

---

## 3. Removed endpoints (−7)

- `POST /sync/delta-sync` & `POST /sync/full-sync` — legacy mobile sync gone,
  replaced by Sync v2 over the existing `/sync/stream` (see §5). Schemas
  `AssetDeltaSyncDto/ResponseDto`, `AssetFullSyncDto` deleted.
  *(Adapter already marks both deprecated at `routers/api/sync/routes.py:305,373`.)*
- `POST /assets/exist` — `CheckExistingAssetsDto/ResponseDto` deleted.
  *(Adapter implements at `routers/api/assets.py:295`.)*
- `GET /assets/random` — use `POST /search/random`.
- `GET /assets/device/{deviceId}` — *(adapter `routers/api/assets.py:1062`, already deprecated.)*
- `PUT /assets/{id}/original` — asset original-file replace removed (only method drop on a retained path).
- `GET /server/theme` — `ServerThemeDto` deleted.
- `GET /plugins/triggers` — replaced by `/plugins/methods` + `/plugins/templates`.

---

## 4. New endpoints (+17), by feature area

- **Adaptive video streaming (HLS)** — `GET /assets/{id}/video/stream/main.m3u8`,
  `…/{sessionId}/{variantIndex}/playlist.m3u8`, `…/{sessionId}/{variantIndex}/{filename}`
  (segments), `DELETE …/{sessionId}`. Backed by `ServerFeaturesDto.realtimeTranscoding`,
  `SystemConfigFFmpegRealtimeDto`, `JobName.HlsSessionCleanup`.
- **Integrity checks (admin)** — `/admin/integrity/report`, `/report/{id}`,
  `/report/{id}/file`, `/report/{type}/csv`, `/summary`. New schemas
  `IntegrityReport*`, `SystemConfigIntegrityChecks(umJob/Job)`,
  `SystemConfigJob.integrityCheck`, `QueueName.integrityCheck`, plus ~21 new
  integrity `JobName`/`ManualJobName` values.
- **Calendar heatmap** — `GET /users/me/calendar-heatmap`,
  `GET /admin/users/{id}/calendar-heatmap`; `CalendarHeatmapResponseDto`,
  `CalendarHeatmapType`.
- **Albums** — `GET /albums/{id}/map-markers`.
- **OAuth backchannel logout** — `POST /oauth/backchannel-logout`;
  `OAuthBackchannelLogoutDto`; `SystemConfigOAuthDto.endSessionEndpoint`/
  `allowInsecureRequests`/`prompt`.
- **Plugins / Workflows (experimental, restructured)** — `/plugins/methods`,
  `/plugins/templates`, `/workflows/triggers`, `/workflows/{id}/share`. Model
  moved from `actions`/`filters` → `steps`/`methods`/`trigger`
  (`Workflow*Dto`, `Plugin{Method,Template,TemplateStep}ResponseDto`,
  `WorkflowTrigger/Type/Step*` added; `*ActionItem*`, `*FilterItem*`,
  `PluginTriggerType`, `PluginContextType` removed).

---

## 5. Sync v2 (same paths, new message types)

No new `/sync` paths — `/sync/stream` + `/sync/ack` now carry **V2 variants**:

- `SyncRequestType` +`AssetsV2`, `AlbumsV2`, `AlbumAssetsV2`, `PartnerAssetsV2`,
  `AssetFacesV2`, `AssetOcrV1`.
- `SyncEntityType` (→59 values) +`AssetV2`, `AlbumV2`, `PartnerAssetV2`/
  `PartnerAssetBackfillV2`, `AlbumAsset{Create,Update,Backfill}V2`, `AssetFaceV2`,
  `AssetOcrV1`/`AssetOcrDeleteV1`.
- New payloads: `SyncAssetV2`, `SyncAlbumV2`, `SyncAssetOcrV1`/
  `SyncAssetOcrDeleteV1`. `SyncAssetV1` gains required `createdAt`;
  `SyncAuthUserV1`/`SyncUserV1` `avatarColor` no longer required.

v3 mobile clients will negotiate V2 + OCR entities, so this is meaningful work in
`routers/api/sync/` (converters, types, stream).

---

## 6. Changed params, behavior, and deprecations

### Changed query params / behavior

- **`GET /albums`**: `shared` (bool) replaced by `isOwned`, `isShared`, `id`,
  `name` filters.
- **`GET /albums/{id}`**: lost `withoutAssets`.
- **`GET /timeline/bucket` & `/buckets`**: added `orderBy` (`AssetOrderBy`).
- **`GET /plugins` & `/workflows`**: added rich filter params (`id`, `name`,
  `enabled`, `description`, `trigger`, …).
- **Face clustering config**: `PeopleResponse`/`PeopleUpdate` add `minimumFaces`;
  `ServerConfigDto` adds `minFaces`.

### New required response fields (clients must emit/tolerate)

`ServerVersionResponseDto.prerelease`, `ServerFeaturesDto.realtimeTranscoding`,
`SystemConfigNewVersionCheckDto.channel` (`ReleaseChannel`),
`TimeBucketAssetResponseDto.createdAt`. (`city`/`country` became optional on
`TimeBucketAssetResponseDto`.)

### Deprecated-in-place (still functional in RC)

These PUTs are now `deprecated: true` but **no PATCH replacement exists in the
RC** (no methods were added) — likely pre-announcing a future PATCH migration:

`PUT` on `/assets`, `/assets/{id}`, `/admin/users/{id}`,
`/admin/users/{id}/preferences`, `/api-keys/{id}`, `/libraries/{id}`,
`/memories/{id}`, `/people/{id}`, `/sessions/{id}`, `/stacks/{id}`, `/tags/{id}`,
`/users/me`, `/users/me/preferences`, `/workflows/{id}`.

### Enum housekeeping

`AlbumUserRole` +`owner`; `AudioCodec` −`libopus`; `JobName` −`AuditLogCleanup`/
`WorkflowRun`, +integrity/HLS/`WorkflowAssetTrigger` values.

---

## 7. Adapter retarget plan (priority order)

1. **`duration` → integer ms** (`asset_conversion.py`, `models.py`,
   `immich_models.py`) — touches every asset/timeline/upload response.
2. **`AlbumResponseDto`** — derive owner from `albumUsers[0]`, drop
   `owner`/`ownerId`/inline `assets`.
3. **Sync v2** — handle V2 request/entity types + OCR in `routers/api/sync/`.
4. **Shared links** — token/key/slug access model rework.
5. **`AssetResponseDto`** — drop device fields + `unassignedFaces`, switch
   `people` to `PersonResponseDto`.
6. **Compat decisions** — whether to keep the 7 removed endpoints as shims
   (several already deprecated in the adapter) and the deprecated PUTs.
7. **New feature areas** (HLS streaming, integrity, calendar heatmap,
   plugins/workflows) — likely stub/skip initially for the adapter.

---

## Open questions

- Does the adapter retarget 3.0 GA in one cut, or run a compatibility window
  supporting both 2.7.5 and 3.0 clients? (Affects whether removed endpoints and
  string-duration stay as shims.)
- Are 3.0 mobile clients hard-requiring Sync v2, or do they fall back to V1
  entity types? Determines whether §5 is blocking or incremental.
- Which new feature areas (if any) are in scope vs. permanent intentional gaps —
  see the companion gap analysis in `immich-adapter-gap-analysis.md`.
