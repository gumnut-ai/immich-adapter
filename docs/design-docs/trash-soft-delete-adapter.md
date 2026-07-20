---
title: "Trash: Soft-Delete with Retention (Adapter)"
status: completed
created: 2026-04-20
last-updated: 2026-07-20
---

# Trash: Soft-Delete with Retention (Adapter)

## Context

The adapter now implements Immich's trash flow on top of the Gumnut API soft-delete primitives. Immich clients still call the native `DELETE /api/assets` endpoint with the `force` flag plus the existing `/api/trash/*` routes; the adapter translates those calls into backend trash, restore, and permanent-delete operations without changing the public wire contract.

This work spans delete semantics, trash endpoints, timeline/statistics filters, sync `deletedAt` propagation, WebSocket events, and the `trashDays` value shown in the web app. This doc records the shipped adapter behavior and the remaining deliberate limitations.

## Implemented behavior

### Delete semantics

`DELETE /api/assets` now branches on Immich's `force` flag:

- `force=false` or omitted: soft-delete via `POST /api/assets/trash`
- `force=true`: permanent delete via bulk `DELETE /api/assets`

Both paths batch requests by `BULK_CHUNK_SIZE`. Soft-delete emits one `on_asset_trash` event per chunk carrying the full id array. Permanent delete emits one `on_asset_delete` event per id, matching Immich's wire shape for hard deletes.

The backend trash and delete endpoints are idempotent for already-transitioned ids, so the adapter does not need the old per-id 404 swallowing loop.

### Trash endpoints

| Endpoint | Current behavior | Count semantics |
|----------|------------------|-----------------|
| `POST /api/trash/restore/assets` | Restores the requested ids in chunks via `POST /api/assets/restore` | Returns `len(request.ids)` because the upstream restore endpoint returns `204` with no per-row count |
| `POST /api/trash/restore` | Enumerates the caller's `state="trashed"` assets, then restores them in chunks | Returns the upfront enumerated id count; concurrent changes can make the exact number of transitioned rows slightly smaller |
| `POST /api/trash/empty` | Enumerates the caller's `state="trashed"` assets, then permanently deletes them in chunks | Returns the upfront enumerated id count; concurrent changes can make the exact number of transitioned rows slightly smaller |

The restore-all and empty-trash flows collect the trashed id list before mutating anything so the pagination cursor stays stable while the `state="trashed"` result set shrinks.

### Trash-aware read paths

Trash state is now visible across the adapter's main read surfaces:

- `GET /api/timeline/buckets?isTrashed=true` passes `state="trashed"` to the monthly counts query.
- `GET /api/timeline/bucket?isTrashed=true` passes `state="trashed"` to asset listing and populates each response entry's `isTrashed` value from that asset's `trashed_at` field.
- `GET /api/assets/statistics?isTrashed=true` passes `state="trashed"` through to the backend listing path.
- `AssetResponseDto.isTrashed` and the upload-ready WebSocket payload's `asset.deletedAt` are sourced from `trashed_at`, not hardcoded placeholders.

Live read paths continue to use the backend's default live-only filtering when `isTrashed` is absent or false.

### Sync stream and WebSocket propagation

Trash state now flows through both client update channels:

- `SyncAssetV1.deletedAt` is populated from `trashed_at`.
- Sync asset hydration uses `client.assets.list(state="all", ids=...)` so `asset_trashed` events can still hydrate after the asset leaves the live view.
- `on_asset_trash` and `on_asset_restore` carry batched id arrays; `on_asset_delete` remains the permanent-delete event and carries one id per emission.

This preserves the intended Immich behavior: trash/restore remain upserts in the sync stream, while permanent delete continues to use the delete path.

### Server config

`GET /api/server/config` now reads `trashDays` from `TRASH_RETENTION_DAYS` through `get_settings().trash_retention_days`. The web trash page therefore shows the deployed retention period instead of a hardcoded placeholder.

The adapter and backend still need to agree on the same deploy-time `TRASH_RETENTION_DAYS` value; `.env.example` and the README document that contract.

## Remaining limitations

- The typed SDK still does not expose dedicated trash helpers, so the trash router uses `AsyncGumnut.post()` / `.delete()` directly for the restore and bulk-delete endpoints.
- `search/metadata` and `search/large-assets` still do not surface trashed results through `withDeleted`, `trashedBefore`, or `trashedAfter`; that follow-up remains separate from the shipped trash flow.
- `trashDays` is accurate at deploy time, but it is still a shared environment-variable contract rather than a backend-discovered runtime setting.

## Verification

Key automated coverage for this area lives in:

- `tests/unit/api/test_assets.py` — `DELETE /api/assets` soft-delete vs. hard-delete branching and WebSocket emission shape
- `tests/unit/api/test_trash.py` — restore-by-ids, restore-all, empty-trash, chunking, and error handling
- `tests/unit/api/test_timeline.py` — `isTrashed` bucket/count routing and per-asset trash flags
- `tests/unit/api/sync/test_trash_propagation.py` — sync `deletedAt` propagation and `state="all"` hydration
- `tests/unit/utils/test_asset_conversion.py` — `AssetResponseDto.isTrashed` and upload-ready `deletedAt`
- `tests/integration/test_server_config.py` — `trashDays` default and env override

## Dependencies

This adapter behavior relies on the following backend capabilities:

- asset-level trash state via `trashed_at`
- bulk trash/restore/permanent-delete endpoints
- asset listing/counting with `state="trashed"` and `state="all"`
- distinct trash, restore, and permanent-delete asset events for the sync stream and realtime channels
- a shared `TRASH_RETENTION_DAYS` deployment contract

## Evolution Notes

- **2026-07-20**: Moved trash mutation chunking to the shared `GUMNUT_API_MAX_BULK_IDS` contract so trash, album, asset-update, and sync hydration calls follow the same Gumnut API bulk limit.
