---
title: "Trash: Soft-Delete with Retention (Adapter)"
status: draft
created: 2026-04-20
last-updated: 2026-04-20
---

# Trash: Soft-Delete with Retention (Adapter)

## Context

The backend half of this work is designed in `photos/docs/design-docs/trash-soft-delete-retention.md` (GUM-556) and already merged as a design. It adds a nullable `deleted_at` column on `assets`, splits `AssetService.delete_assets()` into `trash_assets` / `restore_assets` / `purge_assets`, emits new `ASSET_TRASHED` / `ASSET_RESTORED` events, filters trashed rows from every user-facing query, and schedules a retention-driven purge task. The backend also exposes a privileged ids-only fetch that includes trashed rows (for sync hydration) and exposes `deleted_at` on `AssetResponse`.

This doc covers the adapter half. Today:

- `DELETE /api/assets` (`routers/api/assets.py:609`) hard-deletes via `client.assets.delete`. The `force` parameter on `AssetBulkDeleteDto` is silently ignored (see `assets.py:617`).
- `POST /api/trash/empty`, `POST /api/trash/restore`, and `POST /api/trash/restore/assets` (`routers/api/trash.py`) all return `TrashResponseDto(count=0)` as stubs.
- `GET /api/timeline/buckets` and `GET /api/timeline/bucket` (`routers/api/timeline.py:79-84`, `timeline.py:255`) return an empty list whenever `isTrashed=true` and hardcode `isTrashed=False` on every bucket entry — so the web trash page is permanently empty.
- `GET /api/assets/statistics` (`routers/api/assets.py:693`) accepts an `isTrashed` query param but ignores it.
- `POST /api/search/metadata` and `POST /api/search/large-assets` accept `withDeleted` / `trashedBefore` / `trashedAfter` and ignore them.
- `AssetResponseDto.isTrashed` is hardcoded to `false` in `convert_gumnut_asset_to_immich` (`routers/utils/asset_conversion.py:357`).
- `SyncAssetV1.deletedAt` is hardcoded to `None` in both `gumnut_asset_to_sync_asset_v1` (`routers/api/sync/converters.py:162`) and `build_asset_upload_ready_payload` (`routers/utils/asset_conversion.py:266`).
- The adapter emits `WebSocketEvent.ASSET_DELETE` (`on_asset_delete`) on every delete (`assets.py:630-643`), regardless of `force`. The Immich UI uses `on_asset_trash` for soft-deletes and `on_asset_delete` only for permanent deletes; conflating the two causes the web client to remove the asset from the timeline immediately even on a soft delete, where a restore should have been possible.

The Immich wire contract is public and consumed by unmodified web and mobile clients — we cannot change endpoint shapes. Internal plumbing is ours.

## Goals

- `DELETE /api/assets` honors `force`: `force=true` permanently deletes, `force=false` (the default) soft-deletes via the backend's new `trash_assets` service.
- `/api/trash/empty`, `/api/trash/restore`, `/api/trash/restore/assets` operate against the backend's new trash primitives and return real counts.
- The timeline, search, statistics, and MCP-adjacent read paths correctly partition live vs. trashed assets: live reads never surface trashed entities, the web trash page shows trashed entities, and `isTrashed` on every asset DTO reflects `deleted_at`.
- The sync stream surfaces trash/restore transitions to mobile clients as `SyncAssetV1` upserts carrying `deletedAt`, and permanent deletes as `SyncAssetDeleteV1`. This requires hydrating `ASSET_TRASHED` events via the backend's new ids-only privileged fetch so they are not dropped by the default live-only filter.
- The WebSocket realtime channel emits `on_asset_trash` on trash, `on_asset_restore` on restore, and keeps `on_asset_delete` reserved for permanent deletes — matching Immich's native contract.
- Server config (`trashDays`) reflects the backend's configured retention.

Non-goals:

- Changing any Immich-facing endpoint shapes or query-parameter semantics.
- Adding a separate "trash" storage path, CDN behavior, or thumbnail variant — originals and thumbs stream unchanged until the backend purge runs.
- Partner-shared trash. Partner sharing is out of scope across the adapter today; trash state for partner assets is deferred.
- Admin-surface trashed-asset stats (`/api/admin/users/{id}/statistics` `isTrashed` filter remains stubbed at 0/0/0 as it is today).

## Recommended Approach

### Delete endpoint: honor `force`

`DELETE /api/assets` currently calls `client.assets.delete(asset_id)` per id, which maps to photos-api `DELETE /api/assets/{id}` — which, after the backend change, is a soft-delete. So a no-op adapter behaves like `force=false` by accident. We need explicit behavior for both values.

Split the flow in `delete_assets` (`routers/api/assets.py:609`):

- `force=False` (Immich default): call the backend's `trash_assets` endpoint with the full id list in a single request. Backend returns the ids that actually transitioned (live → trashed). For each transitioned id, emit a WebSocket `on_asset_trash` event; see *WebSocket events* below.
- `force=True`: call the backend's `purge_assets` endpoint with the full id list. Backend returns the ids that were actually purged. Emit `on_asset_delete` per purged id.

Both paths replace the current per-id loop: today's code issues one `client.assets.delete(...)` per id and swallows 404s individually. The new paths should issue one bulk call per request and preserve the "return 204 and keep going" semantics by letting the backend skip ids it cannot transition (it already no-ops on already-trashed / already-deleted, per the backend design's `UPDATE ... WHERE deleted_at IS NULL RETURNING id` contract). Partial failures at the network layer still map through `map_gumnut_error` and surface as a single adapter 5xx to the client.

The exact REST shape on the backend side — `POST /api/assets/trash` vs. `DELETE /api/assets` with a `force` query param, etc. — is an open item in the backend design. The adapter uses whatever the backend lands on via the Gumnut SDK; it does not need its own wire design.

### Trash endpoints

`routers/api/trash.py` is replaced end to end:

- `POST /api/trash/restore/assets` → call backend `restore_assets` with the DTO id list. Emit `on_asset_restore` for every id the backend reports restored. Return `TrashResponseDto(count=<restored>)`.
- `POST /api/trash/restore` → restore **all** of the caller's trashed assets. Immich's native `TrashService.restore(auth)` takes no id list (see `immich/server/src/services/trash.service.ts:27`). The backend exposes `list_trashed_assets()` per-library; the adapter enumerates the caller's trashed ids (paginated) and calls `restore_assets` with them. Return the total restored count, and emit `on_asset_restore` in batches so the event payload stays bounded.
- `POST /api/trash/empty` → purge **all** of the caller's trashed assets. Same enumeration pattern: list trashed ids, then call the backend's `purge_assets`. Return the count purged. Emit `on_asset_delete` per purged id.

Immich's native `POST /trash/empty` returns immediately and queues the deletions to a background job (`TrashService.empty` → `AssetEmptyTrash` queue). The backend's `purge_assets` is also async-safe: it commits the DELETE + outbox together, and storage/CDN cleanup runs on `on_commit` via `AssetStorageCleanupTask`. So the adapter can call `purge_assets` synchronously and return the count; the user-visible behavior (assets disappear from the trash view immediately, storage frees shortly after) is indistinguishable from Immich's native behavior.

Enumeration batching: trash lists can be large (a user who empties trash after 90 days of accumulation). Enumerate in pages using the backend's `list_trashed_assets` pagination, and call `restore_assets` / `purge_assets` per page rather than accumulating into a single giant bulk call. A single logical "empty trash" may issue multiple backend calls; that is fine because the operation is append-only (ids not purged yet in a later page are still purgeable later if the request is retried).

If the backend enumeration loop is interrupted partway through, the outcome is a partial empty/restore — ids that made it into a completed batch are transitioned, ids that did not remain trashed and will be picked up on the next invocation. That matches Immich's native behavior under failure and requires no compensating state on the adapter side.

### Timeline endpoints: honor `isTrashed`

Two call sites in `routers/api/timeline.py`:

**`GET /api/timeline/buckets`** (`timeline.py:61`): currently returns `[]` when `isTrashed=true` (`timeline.py:79-84`). Replace that short-circuit with a call to the backend counts endpoint in "trashed-only" mode. The backend's counts endpoint filters live assets by default via `exclude_trashed()`; we need it to accept an `is_trashed=true` filter (or a dedicated `trashed_counts` endpoint) that inverts the filter. This is a small backend addition that rides on this work but is called out separately so the backend PR can add it explicitly.

Leave the `isFavorite` and `visibility != timeline` short-circuits alone — those are orthogonal gaps tracked in `immich-adapter-gap-analysis.md`.

**`GET /api/timeline/bucket`** (`timeline.py:114`): currently hardcodes `isTrashed: [False] * asset_count` (`timeline.py:255`). Update the asset fetch to respect the `isTrashed` param:

- When `isTrashed=true`, fetch from the trashed-assets listing endpoint scoped to the month window (same date filtering as today).
- When `isTrashed` is unset or false, fetch from the existing live listing (which already excludes trashed rows after the backend change).
- Populate `isTrashed` in the response array from each fetched asset's `deleted_at` (truthy → True), not a hardcoded constant.

This is what makes the web trash page (`immich/web/.../trash/+page.svelte`) actually populate: the page calls `Timeline` with `{ isTrashed: true }`, which cascades into time-bucket queries with `isTrashed=true`.

The web trash page also reads `serverConfigManager.value.trashDays` to show "trashed items will be permanently deleted after N days." See *Server config* below.

### Asset statistics: honor `isTrashed`

`GET /api/assets/statistics` (`routers/api/assets.py:693`) takes `isTrashed` and `visibility` and currently ignores both. The web user-usage stats page calls `getAssetStatistics({ isTrashed: true })` to show "trashed" totals.

Route the filter through to the backend counts/stats endpoint. When the backend exposes a trashed-counts mode (same addition as timeline buckets above), pass it through. When `isTrashed=false` or unset, keep the current behavior (counts over live assets only, now correctly filtering trashed rows because the backend added `exclude_trashed()`).

### Search: `withDeleted` and `trashedBefore` / `trashedAfter`

`POST /api/search/metadata` (`search.py:158`) and `POST /api/search/large-assets` (`search.py:56`) accept `withDeleted`, `trashedBefore`, and `trashedAfter` in their DTOs and ignore them today.

For this work we only wire up the minimum needed to avoid regressions:

- Leave `search/metadata` behavior unchanged for `withDeleted=false` (default). The backend's `search_service` adds `exclude_trashed()` in this work, so trashed assets are correctly excluded from search results without adapter changes.
- For `withDeleted=true` we do **not** expand the search to trashed assets in this PR; the Immich web app does not currently pass `withDeleted=true` from any user-reachable search surface (verified against `immich/web/src`), so the user-visible behavior is unchanged. `trashedBefore` / `trashedAfter` remain stubbed for the same reason. Called out explicitly so a future "expose trashed assets in search" task knows this was deferred.

`search/large-assets` remains a stub for orthogonal reasons (Gumnut doesn't track file size); no change here.

### Asset DTO conversion: populate `isTrashed` and `deletedAt`

Three conversion sites need updating, all in `routers/utils/asset_conversion.py` and `routers/api/sync/converters.py`. All three depend on the backend's change to expose `deleted_at` on its `AssetResponse`.

- `convert_gumnut_asset_to_immich` (`asset_conversion.py:288`): replace the hardcoded `isTrashed=False` with `isTrashed=bool(gumnut_asset.deleted_at)`. This shows up in the Immich UI as the "In trash" indicator and gates the restore-vs-delete action bar.
- `gumnut_asset_to_sync_asset_v1` (`sync/converters.py:120`): replace the hardcoded `deletedAt=None` with `deletedAt=gumnut_asset.deleted_at`. This is what the sync stream ships to mobile as the trash-state signal (see *Sync stream* below).
- `build_asset_upload_ready_payload` (`asset_conversion.py:229`): same change. The `AssetUploadReadyV1` WebSocket event fires on upload success; `deletedAt` will almost always be null there (you can't upload a pre-trashed asset), but the field must be populated from the source of truth so we don't carry a future foot-gun.

### Sync stream: trash/restore as `SyncAssetV1`, permanent delete as `SyncAssetDeleteV1`

The backend emits three distinct events: `ASSET_TRASHED`, `ASSET_RESTORED`, and `ASSET_DELETED` (reserved for permanent purge). The Immich mobile client has only two asset event shapes in the sync stream: a `SyncAssetV1` upsert (carrying `deletedAt: Date | null`) and a `SyncAssetDeleteV1` hard-delete. The mapping:

| photos-api event | Immich sync event | Adapter path |
|---|---|---|
| `asset_trashed` | `SyncAssetV1` upsert | upsert path in `_stream_entity_type` |
| `asset_restored` | `SyncAssetV1` upsert | upsert path in `_stream_entity_type` |
| `asset_deleted` | `SyncAssetDeleteV1` | delete path, already wired (`events.py:141`) |

Mechanically this is already almost right: `_stream_entity_type` (`routers/api/sync/stream.py:110`) classifies events by `event_type` against `_DELETE_EVENT_TYPES` (`stream.py:54`). `asset_trashed` and `asset_restored` are not in that set, so they will fall into the upsert branch naturally once they start arriving. The events will hit `fetch_entities_map` (`entity_fetch.py:20`) to hydrate full asset state, and the converter already ships `deletedAt` once we update it (see *Asset DTO conversion*).

One hydration hazard: the default `client.assets.list(ids=...)` is subject to the backend's `exclude_trashed()` filter, so an `asset_trashed` event arrives in the stream, the adapter tries to hydrate it, the backend returns no row, and the adapter drops the event with a "likely deleted between event fetch and entity fetch" warning (`stream.py:269`). The mobile client never learns the asset was trashed until it is eventually purged, at which point it disappears entirely — skipping the restore window.

Use the backend's privileged ids-only fetch (`list_assets_including_trashed` per the backend design) for asset hydration in the sync stream. This is a one-line change in `entity_fetch.py` — the `asset` branch switches from `gumnut_client.assets.list(ids=chunk, ...)` to the privileged variant. The privileged path is scoped to the sync stream and does not leak into the live-timeline callers, which still want trashed rows filtered.

The `exif` branch in `entity_fetch.py:79` also fetches via `assets.list`. `asset_trashed` does not emit an exif event, and `exif_updated` for a trashed asset is rare (the user can't edit exif on a trashed asset in Immich), so we can leave the exif branch on the default filter. If an exif event on a trashed asset ever arrives, the existing "explicitly missing" logging path will surface it and we can revisit.

FK verification: `extract_payload_fk_refs` (`fk_integrity.py:213`) does payload-ref verification via `fetch_entities_map` to catch cross-cycle deletes. The same privileged path change applies there: when verifying payload-referenced asset IDs (e.g. an album's `album_cover_asset_id` that points at a now-trashed asset), we need to see trashed rows so we don't null out an album cover just because it's in trash. The album cover is a valid reference while trashed; only a purged asset should null it. This is consistent with the privileged fetch being "the sync-stream canonical fetch path," not a case-by-case opt-in.

Event ordering within the two-phase stream is unchanged: `asset_trashed` / `asset_restored` are upserts, so they ride in Phase 1 with other `AssetV1`s. `asset_deleted` stays as a delete, so it rides in Phase 2 in reverse FK order alongside other `AssetDeleteV1`s. No changes to `_DELETE_TYPE_ORDER` (`stream.py:88`) or `_SYNC_TYPE_ORDER` (`stream.py:74`) are needed.

### WebSocket events: `on_asset_trash` and `on_asset_restore`

Add two entries to `WebSocketEvent` in `services/websockets.py:32`:

```python
ASSET_TRASH = "on_asset_trash"
ASSET_RESTORE = "on_asset_restore"
```

Immich's wire contract (`immich/server/src/repositories/websocket.repository.ts:25-29`) defines:

- `on_asset_delete: [string]` — single asset id, permanent delete
- `on_asset_trash: [string[]]` — array of asset ids, soft-delete
- `on_asset_restore: [string[]]` — array of asset ids, restore

Note the shape difference: `on_asset_delete` is a single id per event, `on_asset_trash` / `on_asset_restore` are arrays. The adapter's bulk delete path should emit **one** `on_asset_trash` event with the full array (not N events, one per id), matching Immich's batched semantics. Same for restore. For permanent delete (`force=true` and the `/trash/empty` flow) we keep emitting per-id `on_asset_delete` to match the existing wire shape.

`emit_user_event` (`services/websockets.py`) already accepts arbitrary payload shapes; no signature changes. Callers in `delete_assets` and the new trash-router handlers dispatch the events after the backend call returns success.

The WebSocket events are independent of the sync stream: they drive the web client's live timeline updates and the mobile "something happened" indicator, while the sync stream drives the mobile client's durable local DB. Both channels must fire on every transition.

### Server config: `trashDays`

`fake_config["trashDays"]` in `routers/api/server.py:53` is currently hardcoded to `30`. The backend's default is `TRASH_RETENTION_DAYS=90`, and the value is deploy-configurable.

Simplest path: add an adapter-side env var `TRASH_RETENTION_DAYS` (defaulting to 90 to match the backend) and plumb it through `config/settings.py` into `fake_config` at request time (not module-import time, so env changes pick up on restart). The two values — backend and adapter — must be kept in sync via deploy config; they live in separate processes, so the adapter cannot read the backend's settings directly, but both read the same env var name in practice.

Alternative considered: add a photos-api endpoint that exposes `trash_retention_days`. Not worth the round-trip for a value that changes only on deploy; env-var sync is sufficient.

## Alternatives Evaluated

| Approach | Verdict | Key reason |
|----------|---------|-----------|
| Map `force=true/false` to the backend's split service methods; use privileged fetch in sync hydration; populate `isTrashed` / `deletedAt` in all DTOs | **Recommended** | Matches Immich wire contract, reuses backend primitives, minimal new adapter surface |
| Keep adapter stateful about trash (shadow list of trashed ids in adapter) to avoid depending on backend privileged fetch | Not recommended | Adds an independent source of truth that can drift from the backend; every sync call would need a reconciliation step |
| Emit per-id `on_asset_trash` WebSocket events (one per id) instead of a single batched event | Not recommended | Diverges from Immich's `on_asset_trash: [string[]]` shape; web client would receive N updates for one user action |
| Re-map `ASSET_TRASHED` to `SyncAssetDeleteV1` on the wire to avoid hydration hazards | Not recommended | Mobile can't distinguish trash from permanent delete; restore window is invisible; Android's local-file trash handling (keyed off `deletedAt` non-null) would never fire |
| Hard-delete on `force=false` as a "minimum viable" shim until the backend ships | Not recommended | That is literally today's bug; the whole point is to close it |
| Expose a dedicated `/api/trash/*` set of gumnut endpoints on the adapter backend instead of parameter-flavored existing endpoints | Not recommended | The Immich wire already has the trash endpoints; the backend shape is its own concern (backend design defers it) |

## Migration / Rollout Plan

Depends on backend PR 1 (schema + service methods) landing first. Backend PR 2 (the default-delete behavior cutover) and the adapter PR below can ship in either order, because:

- Until backend PR 2 lands, `DELETE /api/assets/{id}` on the backend still hard-deletes. The adapter's `force=false` branch calls the new `trash_assets` service, which exists after PR 1. Existing per-id `client.assets.delete` callers (if any remain) unchanged.
- Backend PR 2 removes the `delete_assets` wrapper and registers the purge task; it does not change the service-method API the adapter uses.

Suggested adapter PR order (single PR is fine if reviewers prefer; these split out if the diff gets large):

**PR A — DTO conversions + sync stream hydration**

1. Add `deleted_at` passthrough in `convert_gumnut_asset_to_immich`, `gumnut_asset_to_sync_asset_v1`, and `build_asset_upload_ready_payload`.
2. Switch `fetch_entities_map`'s `asset` branch to the privileged (include-trashed) fetch for sync-stream callers. Keep the default `assets.list` for every other caller (`timeline.py`, `albums.py`, `people.py`, `assets.py`, `search.py`).
3. Update `trashDays` in server config to read from env.

Before backend PR 1 ships, `deleted_at` is always null on the wire, so all three changes are no-ops in production; safe to land early.

**PR B — delete endpoint honors `force` + trash router + WebSocket events**

1. Add `WebSocketEvent.ASSET_TRASH` / `ASSET_RESTORE`.
2. Rewrite `delete_assets` to branch on `force` and call `trash_assets` / `purge_assets` (bulk) with matching WebSocket emissions.
3. Rewrite `routers/api/trash.py` to back all three endpoints against backend primitives (enumerate via `list_trashed_assets` for the restore-all and empty-trash flows).

PR B requires backend PR 1 (trash/restore/purge service methods) to be available. If it lands before backend PR 1, the endpoints 5xx on call — safe but broken. Gate PR B merge on backend PR 1 deploy.

**PR C — timeline/statistics `isTrashed` + search `withDeleted` pass-through**

1. Replace the `isTrashed=true → []` short-circuits in `get_time_buckets` and wire trashed counts.
2. Populate real `isTrashed` values in `get_time_bucket` responses and branch the asset fetch based on `isTrashed`.
3. Pass `isTrashed` through to the backend in `get_asset_statistics`.

Depends on the backend adding a trashed-counts / trashed-listing pagination endpoint (small addition, called out above).

Rollback: all three PRs are revertible independently. PR A is pure additive — reverting returns the null-everywhere behavior. PR B revert restores the hard-delete-ignores-force behavior (the current bug). PR C revert restores the empty-trash-view behavior. There is no migration or persistent state on the adapter side to roll back.

## Verification

Acceptance criteria:

- From the Immich web UI: deleting an asset (default) moves it to the trash page, where it appears with a Restore affordance. The "In trash" indicator is shown in the asset viewer. Permanently deleting from the trash page removes the asset from the trash view.
- From the Immich mobile app: deleting an asset in the app removes it from the timeline and places it in the app's local trash collection. Android clients with the manage-media permission move the local file to the device trash (this exercises `immich/mobile/.../sync_stream.service.dart:191-203`). Restoring from the adapter-backed trash view returns the asset to the timeline on the next sync.
- `DELETE /api/assets` with `force=true` in the request body permanently deletes: the asset leaves the trash view, a `SyncAssetDeleteV1` event arrives on the next sync, and `on_asset_delete` fires once per id on the WebSocket channel.
- `DELETE /api/assets` with `force=false` soft-deletes: the asset moves to trash, a `SyncAssetV1` upsert with non-null `deletedAt` arrives on next sync, and a single batched `on_asset_trash` event carrying all ids fires on the WebSocket channel.
- `POST /api/trash/restore/assets` with specific ids restores those ids and fires `on_asset_restore`. `POST /api/trash/restore` restores all the caller's trashed assets and returns the total count. `POST /api/trash/empty` permanently deletes all the caller's trashed assets and returns the total count.
- The Immich web trash page (`immich/web/.../trash/+page.svelte`) renders the caller's trashed assets (previously empty). The "trashed items will be permanently deleted after N days" message shows the configured retention, not 30.
- `AssetResponseDto.isTrashed` is `true` for trashed assets and `false` for live assets across `GET /api/assets/{id}`, album asset listings, search results, and timeline bucket responses.
- `GET /api/assets/statistics?isTrashed=true` returns non-zero totals for a user with trashed assets.
- Uploading a file whose checksum matches a trashed asset returns the trashed asset's id with `status: duplicate` and `isTrashed: true` in the bulk-upload-check response — consistent with `immich/web/.../file-uploader.ts:35`. This exercises the backend's intentional no-auto-restore contract; the adapter already propagates whatever the backend returns, but the `isTrashed` flag on the response must be populated from `deleted_at` (not hardcoded).

Tests:

- Unit tests in `tests/unit/api/test_trash.py` (new) for all three trash endpoints: empty path with no trashed assets returns 0; with N trashed ids returns N and fires the right WebSocket event shape.
- Unit test for `delete_assets` branching on `force=true` vs `force=false` — mock the backend SDK, assert the correct service method is called and the correct WebSocket event name fires.
- Integration test (`tests/integration/`) for the end-to-end trash cycle: upload an asset, delete with `force=false`, confirm the sync stream emits a `SyncAssetV1` with non-null `deletedAt`, restore via `/api/trash/restore/assets`, confirm a second `SyncAssetV1` with `deletedAt=null`, delete with `force=true`, confirm `SyncAssetDeleteV1`.
- Sync-stream regression test: an `asset_trashed` event hydrates successfully via the privileged fetch path and reaches the client as `SyncAssetV1` with `deletedAt` set. Without the privileged-fetch switch, this test should fail (entity-not-found skip on hydration).
- Timeline regression test: `GET /api/timeline/buckets?isTrashed=true` returns non-empty when trashed assets exist; `GET /api/timeline/bucket?timeBucket=...&isTrashed=true` returns trashed assets with `isTrashed=[true, ...]` in the parallel array.
- DTO conversion spot check in `test_sync_stream_payload.py`: `SyncAssetV1.deletedAt` is populated from `AssetResponse.deleted_at` when present; null otherwise.
- WebSocket event shape test: on a bulk `DELETE /api/assets` with N ids and `force=false`, exactly one `on_asset_trash` event fires with an array payload of N ids, not N separate events.

## Dependencies

Backend PRs from `photos/docs/design-docs/trash-soft-delete-retention.md`:

- PR 1 (schema + service methods + filtering) — required for adapter PR A and PR B to ship meaningfully.
- PR 2 (default-delete cutover + purge task + config) — required for the timeline trash page to remain useful (without PR 2, `trashDays` is aspirational because nothing ever purges).

Additional backend surface this adapter design requires beyond what the backend doc already scopes:

- A way to paginate the caller's trashed assets by id (backend's `list_trashed_assets`, called out in the backend design) — needed for `/trash/restore` and `/trash/empty`.
- A way to request trashed-only counts for the timeline-buckets `isTrashed=true` flow, and trashed-only counts for `/api/assets/statistics?isTrashed=true`. The backend design mentions filtering trashed rows from the counts endpoint; it does not explicitly add a "trashed-only" inverse. Flag this on the backend PR so the inverse filter rides in the same change, not a follow-up.
- A way to filter the live asset listing by month window while inverting to trashed-only — the adapter's `get_time_bucket` currently uses `client.assets.list(extra_query={local_datetime_after, local_datetime_before})`. The trashed variant needs the same date filtering against trashed rows. Either `assets.list(is_trashed=true, ...)` or `assets.list_trashed(...)` works; the exact shape is the backend design's call.
