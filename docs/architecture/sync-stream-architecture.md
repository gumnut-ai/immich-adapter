---
title: "Sync Stream Architecture"
last-updated: 2026-08-03
---

# Sync Stream Architecture

The sync stream (`routers/api/sync/stream.py`) consumes events from the Gumnut API and converts them to Immich sync format. Key concepts:

## Two-Phase Ordering

The stream yields all upserts first (in FK dependency order per `_SYNC_TYPE_ORDER`), then all deletes (in reverse FK order per `_DELETE_TYPE_ORDER`). This prevents FK constraint violations in the mobile client — parents exist before children reference them, and children are cleaned up before parents are removed. See the [sync stream event ordering design doc](../design-docs/sync-stream-event-ordering.md) for the full design rationale and history.

## Event Classification

Event types are classified into `_DELETE_EVENT_TYPES` (construct delete sync event from event data), `_SKIPPED_EVENT_TYPES` (ignored), and everything else is treated as an upsert (fetch full entity from the Gumnut API). Delete events are buffered during iteration and yielded in phase 2.

## Deletion Events

`_make_delete_sync_event()` maps `entity_id` to a UUID. For junction table deletions (e.g., `album_asset_removed`), the event's `payload` field carries the foreign keys since the record is hard-deleted.

## Face person_id Handling

`face_created` events have person_id nulled out (face detection never assigns a person). `face_updated` events use the causally-consistent person_id from the event payload instead of current entity state. For every face batch, payload person_ids are collected (see `extract_payload_fk_refs`) and verified against production via `people.list` — IDs that return 404 are recorded in `stats.not_found_ids["person"]` and nulled out on the outgoing event, regardless of whether a `PersonV1` checkpoint exists. This prevents stale payload references (from clustering runs that predate a person's deletion) from leaking across sync cycles and causing FK violations on the client.

## User Preferences (minimumFaces)

Gumnut has no per-user preferences, but the v3 client reads `people.minimumFaces`
from the `UserMetadataV1` stream to decide which people appear in the People tab,
defaulting to **3** when absent — which would hide Gumnut clusters of 1–2 faces.
The adapter synthesizes a single `UserMetadataV1` *preferences* row with
`minimumFaces=1` (all other fields mirror the client's defaults). It's emitted
right after `UserV1` (the `userId` FK parent) and keyed off the **same
`user_cursor` as `UserV1`** (the user's `updated_at`) — the payload is derived
purely from the user, so it re-syncs exactly when the user record changes and
otherwise acks once. `value` is the server's nested `UserPreferences` JSON shape
(`value["people"]["minimumFaces"]`), which the client parses via
`Preferences.fromMap`.

Only the `preferences` key is synthesized. The client's other `UserMetadataV1`
keys are deliberately not emitted: onboarding UI is gated on the login response's
`isOnboarded`, not the synced `onboarding` row (no client reader consumes it), so
a synthesized onboarding row would change nothing; and Gumnut has no `license`
concept.

## Album Cover Handling

`album_updated` events use the causally-consistent `album_cover_asset_id` from the event payload instead of the entity's current computed cover (which is derived at fetch time via a lateral join and may reference an asset outside the sync window). Payload cover asset IDs are verified against production the same way face person_ids are — 404s null the cover regardless of `AssetV1` checkpoint state.

## Album Owner Album-User Link (v3)

The Immich v3 `SyncAlbumV2` payload dropped `ownerId`, so the mobile client no
longer derives an album's owner from the album event itself. Instead it builds
the album↔owner relationship from a separate `AlbumUsersV1` stream, and its
album-list query **inner-joins on an owner-role album-user row** — an album with
no such row is filtered out and never displayed, even though it synced into the
client DB. (The v1 `SyncAlbumV1` path carried `ownerId` and the client
synthesized the owner row itself, so the adapter never needed to emit it.)

To cover this, `AlbumUsersV1` is a first-class entry in `_SYNC_TYPE_ORDER` mapped
to the same `album` gumnut entity as `AlbumsV1/V2`, streamed **after** the album
(FK parent) and after the owner `UserV1`. Each album fans out to two sync
entities — the album (`AlbumV1/V2`) and its owner link (`AlbumUserV1`) — both
derived from the same `AlbumResponse`. Gumnut is single-user with no album
sharing, so every album has exactly one album-user: the owner (`role=owner`).

`AlbumUserV1` is listed in `_DERIVED_UPSERT_ONLY_TYPES`, so its pass streams
upserts only (`emit_deletes=False`). Album-user *deletes* are owned by the album
pass: an `album_deleted` event emits `AlbumDeleteV1`, and the client's
`remoteAlbumUserEntity.albumId` FK cascades on album deletion — re-emitting the
delete from the album-user pass would duplicate `AlbumDeleteV1`. No
`AlbumUserDeleteV1` is emitted (Gumnut has no unshare operation).

## Stack Snapshot (StacksV1)

Stacks reach the mobile client two ways, both driven from the initial/full sync:

1. **Asset membership.** Each sync asset row carries its `stackId` — the Gumnut
   `stack_id` FK mapped to the Immich UUID (loose assets stay null). V2 inherits
   it through the V1 delegation. This is what groups burst members under a stack
   on the client.
2. **Stack rows.** `StacksV1` streams `StackV1` rows, handled specially
   (`_stream_stacks`) alongside the user types rather than via the events API —
   the Gumnut backend emits no stack lifecycle events yet, so there is nothing to
   feed the event stream. Each row's `primaryAssetId` is the **effective primary**
   resolved by the shared `resolve_effective_primary`/`hydrate_stack` helpers (the
   exact rule the REST `/stacks` surfaces use), so an auto-detected burst with no
   pinned cover still gets a valid non-null primary. A member-less stack has no
   honest primary and is skipped with a warning. `ownerId` is always the current
   user (Gumnut is single-user; no stack sharing).

The stack rows are emitted **before** the phase-1 asset loop, so a stack row
exists on the client before any asset naming it via `stackId` arrives. (The
mobile client's `stackId` / `primaryAssetId` columns are unenforced indexes, not
FKs, so the circular reference is not order-sensitive in practice — stacks-first
is a deliberate, defensive choice for the asset→stack direction.)

**Scope boundary — upserts only, no delete detection.** With no lifecycle event
stream, `_stream_stacks` re-pages the whole stack table on **every** sync and
emits the rows the client has not yet acked: it orders by `(updated_at, id)` and
skips rows at or before the `StackV1` checkpoint cursor. This is upsert-only
incremental delivery, and it exists for a concrete reason — asset rows carry
`stackId` from current entity state unconditionally, so a burst created *after*
the initial sync would otherwise leave its member assets pointing at a stack row
the client never received. A dangling `stackId` is not benign: the mobile
timeline join (`merged_asset.drift.dart`,
`... AND (rae.stack_id IS NULL OR rae.id = se.primary_asset_id)`) hides **every
frame** of a burst whose stack row is missing. Emitting the new stack keeps the
burst visible and grouped.

Ordering by the ack cursor also makes the pass **resumable**: if a snapshot
truncates mid-stream, the checkpoint holds the last acked row's cursor and the
next sync skips at-or-before it, so the un-streamed tail is not lost. This is
also why a Gumnut API error while hydrating one stack is caught at the call site
and ends the pass gracefully rather than propagating: the snapshot runs *before*
the asset loop, so an unhandled error would suppress assets/albums/faces too —
instead the already-streamed rows are acked, the asset loop still runs, and the
remaining stacks resume next sync.

**Delete detection stays out of scope**, tracked on a separate event-support
track. A dissolved stack's row goes stale but is harmless: unstacking clears each
member's `asset.stack_id` via asset events, and a stack row no asset references
satisfies the `rae.stack_id IS NULL` branch of the join. `PartnerStacksV1` stays
a no-op (partner sharing is unsupported), and `on_asset_stack_update` is **not**
in the supported WebSocket table.

## Adding a New Sync Type Version

When the same gumnut entity type maps to multiple Immich sync versions (e.g., AssetFacesV2 alongside V1), update these files in coordination:

1. `stream.py`: Add the V2 entry to `_SYNC_TYPE_ORDER` (after V1, same gumnut entity type) and a `_V1_SUPERSEDED_BY_V2` entry so V1 is skipped when V2 is also requested (prevents duplicate events). **Extend every `sync_entity_type`-gated payload override in `_stream_entity_type` to also match the V2 type** — the "Face person_id Handling" and "Album Cover Handling" overlays above are gated on the sync entity type, and a V2 type left off silently drops that FK-safety guarantee on the v3 client path (the client streams that entity exclusively as V2).
2. `fk_integrity.py`: Add V2 to the entity's list in `_GUMNUT_TYPE_TO_SYNC_TYPES` so FK checkpoint lookups match regardless of which version was synced — a client that checkpointed under the V2 type otherwise misses the "synced in a prior cycle" skip and logs spurious FK warnings.
3. `converters.py`: Write a V2 converter function alongside the V1 one.
4. `events.py`: Update the converter dispatch in `convert_entity_to_sync_event` to select V1 vs V2 converter based on `sync_entity_type`.
5. `test_sync_stream_ordering.py`: Verify the consistency test handles one-to-many gumnut-type-to-sync-type mappings.

The invariant tests in `test_sync_v2.py` assert that every V2 type in `_SYNC_TYPE_ORDER` is wired into the event dispatch (step 4), the FK checkpoint map (step 2), and — for albums — the cover override (step 1), so a half-wired addition fails a test instead of shipping. Extend them when adding a version.

## No-Op Request Types

Immich sync types that are accepted but have no Gumnut equivalent (e.g., `AssetEditsV1` — we don't support editing) go in `_NOOP_REQUEST_TYPES` in `stream.py`. This prevents "unsupported type" warnings while making the no-op explicit. Do not just add them to `_SUPPORTED_REQUEST_TYPES` without `_SYNC_TYPE_ORDER` — that silently drops them.

The v3 mobile client requests a broad set of these on **every** sync (partner-*, `PartnerStacksV1`, memories-*, `AssetMetadataV1`, `AssetOcrV1`, `AlbumAssetExifsV1`, the V2 partner/album-asset variants) — all no-ops because the adapter has no sync-stream source for them, whether the underlying feature is absent (sharing, OCR) or reachable only through the REST surfaces (memories). They all belong in `_NOOP_REQUEST_TYPES` so the per-sync "unsupported types" warning stays quiet. Two requested types the adapter *does* handle are deliberately **not** no-ops, both handled specially alongside `UsersV1`/`AuthUsersV1` rather than via `_SYNC_TYPE_ORDER`: `UserMetadataV1` (synthesized preferences row — see "User Preferences" above) and `StacksV1` (current-state snapshot — see "Stack Snapshot" above). `PartnerStacksV1` remains a no-op regardless.

## Contract with the Gumnut API

The adapter depends on the events API response shape (`EventsResponse`). Fields like `payload` are typed in the SDK (v0.52.0+) and accessed directly. For backward compatibility with old events that predate a field, check for `None` before use.

## Debugging Immich Mobile Logs

Immich mobile app logs contain Immich UUIDs, not Gumnut IDs. When debugging sync issues from mobile logs, use `routers/utils/gumnut_id_conversion.py` to convert UUIDs to Gumnut IDs (e.g., `face_`, `person_`, `asset_` prefixed) before looking up entities in production via API or MCP tools.
