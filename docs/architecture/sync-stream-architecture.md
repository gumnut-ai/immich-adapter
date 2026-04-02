---
title: Sync Stream Architecture
last-updated: 2026-04-02
---

# Sync Stream Architecture

The sync stream (`routers/api/sync/stream.py`) consumes events from photos-api and converts them to Immich sync format. Key concepts:

## Two-Phase Ordering

The stream yields all upserts first (in FK dependency order per `_SYNC_TYPE_ORDER`), then all deletes (in reverse FK order per `_DELETE_TYPE_ORDER`). This prevents FK constraint violations in the mobile client — parents exist before children reference them, and children are cleaned up before parents are removed. See `docs/design-docs/sync-stream-event-ordering.md` for the full design rationale and history.

## Event Classification

Event types are classified into `_DELETE_EVENT_TYPES` (construct delete sync event from event data), `_SKIPPED_EVENT_TYPES` (ignored), and everything else is treated as an upsert (fetch full entity from photos-api). Delete events are buffered during iteration and yielded in phase 2.

## Deletion Events

`_make_delete_sync_event()` maps `entity_id` to a UUID. For junction table deletions (e.g., `album_asset_removed`), the event's `payload` field carries the foreign keys since the record is hard-deleted.

## Face person_id Handling

`face_created` events have person_id nulled out (face detection never assigns a person). `face_updated` events use the causally-consistent person_id from the event payload instead of current entity state. After payload override, person_id is nulled if the person returned 404 during fetch (deleted entity) and no person checkpoint exists.

## Album Cover Handling

`album_updated` events use the causally-consistent `album_cover_asset_id` from the event payload instead of the entity's current computed cover (which is derived at fetch time via a lateral join and may reference an asset outside the sync window). After payload override, cover is nulled if the asset returned 404 during fetch and no asset checkpoint exists.

## Adding a New Sync Type Version

When the same gumnut entity type maps to multiple Immich sync versions (e.g., AssetFacesV2 alongside V1), update these files in coordination:

1. `stream.py`: Add V2 entry to `_SYNC_TYPE_ORDER` (after V1, same gumnut entity type). Add a guard in the stream loop to skip V1 when V2 is also requested (prevents duplicate events). Update face/entity-specific event handling to match both V1 and V2 sync entity types.
2. `fk_integrity.py`: Add V2 to the entity's list in `_GUMNUT_TYPE_TO_SYNC_TYPES` so FK checkpoint lookups match regardless of which version was synced.
3. `converters.py`: Write a V2 converter function alongside the V1 one.
4. `events.py`: Update the converter dispatch in `convert_entity_to_sync_event` to select V1 vs V2 converter based on `sync_entity_type`.
5. `test_sync_stream_ordering.py`: Verify the consistency test handles one-to-many gumnut-type-to-sync-type mappings.

## No-Op Request Types

Immich sync types that are accepted but have no Gumnut equivalent (e.g., `AssetEditsV1` — we don't support editing) go in `_NOOP_REQUEST_TYPES` in `stream.py`. This prevents "unsupported type" warnings while making the no-op explicit. Do not just add them to `_SUPPORTED_REQUEST_TYPES` without `_SYNC_TYPE_ORDER` — that silently drops them.

## Contract with photos-api

The adapter depends on the events API response shape (`EventsResponse`). Fields like `payload` are typed in the SDK (v0.52.0+) and accessed directly. For backward compatibility with old events that predate a field, check for `None` before use.

## Debugging Immich Mobile Logs

Immich mobile app logs contain Immich UUIDs, not Gumnut IDs. When debugging sync issues from mobile logs, use `routers/utils/gumnut_id_conversion.py` to convert UUIDs to Gumnut IDs (e.g., `face_`, `person_`, `asset_` prefixed) before looking up entities in production via API or MCP tools.
