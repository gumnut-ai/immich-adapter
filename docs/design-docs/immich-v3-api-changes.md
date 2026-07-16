---
title: "Immich v2.7.5 → v3.0 API Change Analysis"
status: active
created: 2026-06-16
last-updated: 2026-07-16
---

# Immich v2.7.5 → v3.0 API Change Analysis

## Context

The immich-adapter now targets Immich **v3.0.3** (pinned in
`.immich-container-tag`). Immich **3.0** shipped as **v3.0.0** GA on 2026-06-30,
and the adapter retarget is complete. This document is a structural diff of the
two OpenAPI specs — 2.7.5 against **v3.0.0-rc.0**, re-validated against the GA
spec (see the GA validation note below) — that records the compatibility work and
the remaining intentional gaps.

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
>   SQL adds `${delta} minute` in 2.7.5 and 3.0 alike), so the GA spec is just
>   documenting the long-standing runtime behavior. The adapter now matches that
>   minute-based unit in `routers/api/assets.py`; the remaining "seconds"
>   wording lives only in the generated 2.7.5 model description in
>   `routers/immich_models.py`, so this spec fix adds no new 3.0 retarget work.
>
> The rc.0-based behavioral analysis below therefore stands unchanged for GA.

> **Current target (v3.0.3).** The v3.0.3 spec retains the v3.0.0 GA endpoint
> surface: 173 paths and 254 operations. It adds three schemas —
> `HlsVideoResolution`, `RecentlyAddedResponse`, and `RecentlyAddedUpdate` —
> which are reflected in the regenerated models and the adapter's hand-built
> responses.

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
4. Record retarget status and remaining gaps.

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
| **`duration` string → integer ms** | `AssetResponseDto`, `TimeBucketAssetResponseDto`, `AssetMediaCreateDto`. Was `"HH:MM:SS.ffffff"` interval string, now integer **milliseconds**, nullable on `AssetResponseDto`. | A `duration_ms` helper in `routers/utils/asset_conversion.py` emits int ms (null when unknown) for `AssetResponseDto` / `TimeBucketAssetResponseDto`; the interval-string `format_duration` is retained for `SyncAssetV1`, whose `duration` stays a string in v3 (`SyncAssetV2` is the int-ms sync variant — see §5). The `duration` fields live in `routers/immich_models.py`. Affects every asset/timeline/upload response. |
| **`AlbumResponseDto` restructured** | Removed `assets`, `owner`, `ownerId`. Owner now derived from **`albumUsers[0]`** (now `minItems:1`; documented ordering: owner first, then auth user if different, rest alphabetical). `shared` now required. | Album responses no longer inline assets or owner. Album conversion must populate `albumUsers[0]` as owner and stop emitting `owner`/`ownerId`/`assets`. |
| **`AssetResponseDto` face/device fields** | Removed `deviceAssetId`, `deviceId`, `unassignedFaces`. `people`: `PersonWithFacesResponseDto` → `PersonResponseDto` (no inline face bounding boxes). | No inline face geometry on assets. Schemas `PersonWithFacesResponseDto` and `AssetFaceWithoutPersonResponseDto` deleted. |
| **Shared-link tokens removed** | `SharedLinkResponseDto.token` gone; `GET /shared-links/me` lost `password`/`token` query params; `SharedLinkEditDto.changeExpiryTime` gone; `PUT /albums/{id}/assets`, `PUT /shared-links/{id}/assets`, `PUT /albums/assets` lost `key`/`slug` params. | Shared-link / anonymous (key/slug) access model changed — `routers/api/shared_links.py` and key/slug access path need rework. |
| **`deviceId`/`deviceAssetId` dropped from search/upload DTOs** | `MetadataSearchDto`, `SmartSearchDto`, `RandomSearchDto`, `StatisticsSearchDto`, `AssetMediaCreateDto`. | Device-based dedup/filtering removed across search + upload. Upload still synthesizes `device_asset_id`/`device_id` (`GUMNUT_UPLOAD_DEVICE_ID` + a per-upload UUID) because the Gumnut API requires them; dedup is checksum-based. |
| **Asset replace removed** | `AssetMediaStatus` lost `replaced`; `Permission` lost `asset.replace`; `AssetMediaReplaceDto` deleted. See also removed `PUT /assets/{id}/original` in §3. | |
| **`LicenseResponseDto`** | Lost `activatedAt`, `activationKey`, `licenseKey`. | Licensing response shape changed. |

---

## 3. Removed endpoints (−7)

**Decision (clean cut to 3.0): all dropped from the adapter.** None of these paths
exist in the v3 spec, so no supported v3 client calls them — the handlers were
removed rather than kept as shims. (`/plugins/triggers` was never implemented in
the adapter, so there was nothing to drop.)

- `POST /sync/delta-sync` & `POST /sync/full-sync` — legacy mobile sync gone,
  replaced by Sync v2 over the existing `/sync/stream` (see §5). Schemas
  `AssetDeltaSyncDto/ResponseDto`, `AssetFullSyncDto` deleted. **Dropped.**
- `POST /assets/exist` — `CheckExistingAssetsDto/ResponseDto` deleted. **Dropped.**
- `GET /assets/random` — use `POST /search/random`. **Dropped.**
- `GET /assets/device/{deviceId}` — was a deprecated stub. **Dropped.**
- `PUT /assets/{id}/original` — asset original-file replace removed (only method
  drop on a retained path; the `GET /assets/{id}/original` download stays). **Dropped.**
- `GET /server/theme` — `ServerThemeDto` deleted. **Dropped** (also removed from
  `auth_middleware.py`'s unauthenticated-path set).
- `GET /plugins/triggers` — replaced by `/plugins/methods` + `/plugins/templates`.
  **N/A** — never implemented in the adapter.

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

### What V2 actually changes

At the payload level, V2 adds almost nothing over V1 (this list is the canonical
V2-vs-V1 delta; later sections reference it rather than restate it). Verified
against the v3.0.0 GA models, not the RC — an earlier draft called `SyncAlbumV2`
byte-identical, but GA dropped a field:

- `SyncAssetV2` is `SyncAssetV1` with **`duration` as integer-milliseconds**
  instead of the interval string — the same change as §2. This is the only
  payload difference for the *asset* entity.
- `SyncAlbumV2` is `SyncAlbumV1` **minus `ownerId`** (the GA model dropped it);
  otherwise identical. Consequence: the owner is no longer carried on the album
  event, so the adapter must emit the owner album-user link on the separate
  `AlbumUsersV1` stream, or the v3 client filters every album out of its list
  (its album query inner-joins on an owner-role album-user row). See the
  "Album Owner Album-User Link" section of the sync-stream-architecture doc.
- `SyncAssetFaceV2` is `SyncAssetFaceV1` **plus `deletedAt` / `isVisible`**
  (both constant for the adapter — Gumnut has no face soft-delete or visibility;
  already handled by the pre-existing faces V2 converter).
- The `PartnerAsset*V2` and `AlbumAsset*V2` entity types reuse `SyncAssetV2`, so
  they inherit only the int-ms `duration`.
- `AssetOcrV1` is a genuinely new entity type (OCR text boxes), but the adapter
  has no OCR data — it emits nothing for it and the client tolerates the absence.

### Client behavior — V1 fallback is version-gated, not negotiated

The mobile client picks V1 vs V2 request types purely from the version the
adapter reports at `GET /server/version` (upstream Immich mobile
`mobile/lib/infrastructure/repositories/sync_api.repository.dart`):
`assetsV2` / `albumsV2` / `albumAssetsV2` / `partnerAssetsV2` and `assetOcrV1`
only when the reported version is `>= 3.0.0`; `assetFacesV2` at `>= 2.6.0`. A
below-client version merely sets a "server out of date" UI banner
(`server_info.provider.dart`) — it never blocks sync. So the reported version is
the lever: report `< 3.0.0` and the client requests the V1 surface the adapter
already serves; report `3.0.x` and it requests V2.

The adapter now reports v3.0.3 (from `.immich-container-tag`), so current clients
request the V2 sync surface. `routers/api/sync/` carries
`gumnut_face_to_sync_face_v2`, the `AssetFacesV2` → `AssetFaceV2` stream mapping,
the `AssetsV2` and `AlbumsV2` converters, the owner link on the separate
`AlbumUsersV1` stream, and "skip V1 when V2 is requested" logic. The v3-only
`AssetOcrV1`, `PartnerAssetsV2`, and `AlbumAssetsV2` requests are accepted as
no-ops because Gumnut has no corresponding data.

**Retarget status:** Sync v2 is complete. The V2 mappings reuse the §2 int-ms
`duration` converter; `AssetV2` carries the integer duration, `AlbumV2` drops
`ownerId`, and the adapter supplies the required v3 owner link and
`SyncAssetV1.createdAt` field.

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

## 7. Adapter retarget status and remaining gaps

1. **Completed — `duration` → integer ms** (`asset_conversion.py`,
   `immich_models.py`) — applied to asset, timeline, upload, and sync responses.
2. **Completed — `AlbumResponseDto`** — derives the owner from `albumUsers[0]`
   and drops `owner`/`ownerId`/inline `assets` from the v3 response shape.
3. **Completed — Sync v2** — reports `v3.0.3`, emits the V2 entity mappings, and
   supplies the separate owner link required by v3 clients.
4. **Remaining gap — Shared links** — token/key/slug access model rework is not
   part of the retarget and remains a separate feature gap.
5. **Completed — `AssetResponseDto`** — drops device fields and
   `unassignedFaces`, and uses `PersonResponseDto` for `people`.
6. **Resolved — compatibility decisions** — the 7 removed endpoints are dropped
   with a clean cut and no shims (see §3); the deprecated-in-place PUTs (§6) stay
   as-is.
7. **Completed — new feature-area scope** — HLS streaming, integrity checks,
   OAuth backchannel logout, and plugins/workflows remain intentional gaps; the
   calendar-heatmap user endpoint is stubbed, and album map markers are
   implemented. See *Immich v3 New Feature Areas — Scope Decisions* in
   `immich-adapter-gap-analysis.md`.

---

## Retarget blocker status

`migration/immichv3` was intentionally red while the API-shape work was landing.
The blockers that made the branch fail import or test collection are resolved on
main and are retained here as a closeout record rather than an active work list:

- **Removed symbols:** the `Action` enum was retargeted to
  `AssetUploadAction`; `Error1` was retargeted to `BulkIdErrorReason`; and the
  `APIKey*` imports were retargeted to the v3 `ApiKey*` names.
- **Generated-model validation:** the model preprocessor strips `pattern` from
  non-string formats (`uuid`, `date-time`, `date`, and `time`) before codegen,
  so UUID and datetime DTOs validate normally under the v3 models.
- **Email validation:** stub partner and user addresses use
  `user@example.com`, which satisfies the regenerated `EmailStr` fields.
- **v3.0.1/v3.0.2 required fields:** the preferences stubs now emit
  `recentlyAdded`, and the realtime FFmpeg config supplies `resolutions` and
  `videoCodecs` while keeping realtime HLS disabled.

The retarget-specific tests cover these construction and wire-shape cases, and
the main-branch retarget checks pass linting, type checking, tests, and API
compatibility validation.

## Open questions

- ~~Does the adapter retarget 3.0 GA in one cut, or run a compatibility window
  supporting both 2.7.5 and 3.0 clients?~~ **Resolved: clean cut to 3.0.** The
  product is in alpha with a handful of testers, so the supported client version
  is set by fiat (Immich mobile + web v3). No dual-support window — removed
  endpoints and string-`duration` need not stay as shims.
- ~~Are 3.0 mobile clients hard-requiring Sync v2, or do they fall back to V1
  entity types?~~ **Resolved: incremental, not blocking.** The client picks V1
  vs V2 by the adapter's reported version, and the V2 payload deltas are small
  (§5). The adapter now reports `v3.0.3` and ships the thin V2 layer that reuses
  the §2 duration converter.
- ~~Which new feature areas (if any) are in scope vs. permanent intentional
  gaps?~~ **Resolved: mostly intentional gaps.** Of the six new areas, four are
  intentional gaps unreachable by our v3 clients (HLS streaming, integrity checks,
  OAuth backchannel logout, plugins/workflows), the calendar-heatmap user endpoint
  is stubbed, and album map markers are implemented. See *Immich v3 New
  Feature Areas — Scope Decisions* in `immich-adapter-gap-analysis.md` for the
  per-area reachability analysis and stub shapes.
