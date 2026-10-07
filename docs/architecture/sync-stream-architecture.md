---
title: "Sync Stream Architecture"
last-updated: 2026-10-07
---

# Sync Stream Architecture

The sync stream (`routers/api/sync/stream.py`) consumes events from the Gumnut API and converts them to Immich sync format. Key concepts:

## Two-Phase Ordering

The stream yields all upserts first (in FK dependency order per `_SYNC_TYPE_ORDER`), then all deletes (in reverse FK order per `_DELETE_TYPE_ORDER`). This prevents FK constraint violations in the mobile client — parents exist before children reference them, and children are cleaned up before parents are removed. See the [sync stream event ordering design doc](../design-docs/sync-stream-event-ordering.md) for the full design rationale and history.

Failure is not isolated per pass: an unhandled fetch error propagates to
`generate_sync_stream`'s top-level handler and ends the generator without
`SyncCompleteV1`, dropping every later pass. Degrade to `missing_ids` only when
skipping cannot strand dependent rows; for stacks, that applies to an
undecodable ID, not an empty member read. Retriable failures must propagate so
the event cursor remains unacknowledged.

## Event Classification

Event types are classified into `_DELETE_EVENT_TYPES` (construct delete sync
event from event data), `_SKIPPED_EVENT_TYPES` (ignored), and everything else
is treated as an upsert (fetch full entity from the Gumnut API). Delete events
are buffered during iteration and yielded in phase 2. An unrecognized event
type is therefore a re-read, which is how `asset_moved_out` and
`asset_moved_in` are handled.

## Repeated Events for One Entity

A first sync has no checkpoint, so it replays the library's whole event history, and one entity can appear in it once per change ever made to it. For the entity types in `_CURRENT_STATE_ENTITY_TYPES` the upsert is built from current state alone, so every one of those events would produce the same row. Their passes emit an entity at its first event and skip the rest, without reading the entity again; a person, album membership or stack a read did not return is likewise not read or reported again, because the feed holds only committed changes and the row is therefore deleted. Metadata is re-read: its read also misses an asset that is trashed or outside the session library, which can change mid-pass. The stream summary logs the skipped count as `repeat_event_skips`.

A skipped event carries no ack. So the client's checkpoint still moves past a run of repeats, the pass emits a `SyncAckV1` line — a no-op the client echoes back — acked under the pass's own sync type at the last skipped cursor: once at the end of the pass, and at a page boundary whenever `_SKIPPED_REPEATS_PER_ACK` repeats have gone unacked. The client sends one ack request per run of same-type lines, so these lines are deliberately sparse. Only repeats of an emitted entity move the checkpoint this way; events for an absent entity never emitted a row and still do not.

Face, asset and album upserts are excluded because they take event-time values from the event payload (see the handling sections below), so two events for one entity can produce different rows. The album-user row derived from album events is current-state too, but its pass is left as it is: albums have few events.

## Deletion Events

`make_delete_sync_event()` maps `entity_id` to a UUID. For junction table deletions (e.g., `album_asset_removed`), the event's `payload` field carries the foreign keys since the record is hard-deleted. The feed is not in commit order, so a later re-add can sort ahead of the removal; the adapter re-reads each removed pair and upserts the current membership instead when it exists.

## Asset Membership Is Read From the Session's Library

An asset can move to another Gumnut library and keep its ID. The Gumnut API
then records `asset_moved_out` in the library the asset left and
`asset_moved_in` in the one it joined. Neither carries a payload or names the
other library, and a move out is not a deletion: the asset may be back before
the event is read.

So for every asset event that is not `asset_deleted` (the two move types and
older `asset_created`, `asset_updated`, and trash events alike) the asset pass
asks the session's library whether it holds the asset now. Every Gumnut call
in a sync is bound to that library (see
[Library scope](adapter-architecture.md#library-scope)), and the pass lists
the event IDs there in every state:

- **Present:** the asset is upserted with its current state.
- **Absent:** the pass emits `AssetDeleteV1`. The mobile client removes the
  asset's faces, EXIF row, and album links with it. Only this session's
  library is affected; a session on the destination library reads the asset
  as present.
- **Read failed or incomplete:** nothing is emitted. The read follows every
  page before an ID counts as absent, and an error ends the stream before the
  cursor is acknowledged.

A by-ID asset read must not decide membership: it is not scoped to a library,
so a user who can read both libraries would still get the moved asset back.

The removal is emitted in feed order during phase 1 rather than buffered for
phase 2, and its ack advances the asset pass's own checkpoint. A later event
that finds the asset back therefore wins, an interrupted stream cannot
acknowledge past an undelivered removal, and a library emptied by moves does
not replay them on every sync. An asset created and permanently deleted
within one window gets a second, harmless `AssetDeleteV1` from its
`asset_deleted` event.

An adapter without this behavior skips the absent asset and leaves it on the
client, so it must not run against a Gumnut API that moves assets.

Two things this pass does not do, which a move depends on the Gumnut API for:

- **Hydrating an arriving asset.** EXIF and face rows are emitted only from
  `metadata` and `face` events in the session library's feed. An asset that
  arrives with only `asset_moved_in` syncs without them, so the API must
  record those events in the destination feed with the move.
- **Stacks.** An absent stack row is still confirmed with a by-ID read (see
  [Stacks](#stacks-stacksv1)), which is not scoped to a library. That is
  correct only while a stack never changes library, so when every asset of a
  stack moves, the API must delete the stack and create a new one in the
  destination rather than move it under its ID.

## Gating Rows Is a State Transition, Never an Omission

A pass that must hide rows from clients (e.g., the face-geometry gate — see
`docs/references/asset-and-media-handling.md`) cannot just skip the upsert: a
client that synced the row before the condition arose keeps its stale copy
forever, and the cursor advances past the discarded event. Emit the hidden
state instead — a visibility flag where the sync type has one, a retraction
delete where it doesn't.

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

## Stacks (StacksV1)

`StacksV1` uses the event cursor and sits before assets in `_SYNC_TYPE_ORDER`.
The mobile timeline hides an asset whose `stackId` names an unknown stack, so a
`stack_created` or `stack_updated` event must emit `StackV1` before its member
assets. The client has no stack foreign key (the column is indexed only); this
is a visibility constraint. Deletes use the inverse order, with `StackDeleteV1`
after asset deletes.

`StackV1.primaryAssetId` is required but an unpinned Gumnut stack has no primary
on its row. Sync therefore asks `list_stacks` for each stack's live member IDs
(`include=asset_ids`), applies the shared effective-primary rule, and carries
only the resulting UUID into the converter. A stack with a live member and no
trashed pin costs no further request, which keeps a first sync inside the
upstream rate limit. Only a pin outside the live members or a stack with no
live member needs a concurrency-bounded member read. A row without member IDs
or an empty all-state member read is retryable, because the row still exists;
propagating the error truncates the stream before its cursor is acked.

A stack row **absent from the bulk `list_stacks` read** is likewise retriable —
skipping it would advance the cursor past the stack while the asset pass still
stamps `stackId` on its members, hiding the burst on the mobile timeline. The
guard first excuses exactly the ids with a `stack_deleted` event in the window:
first the current events page, then a look-ahead scan of the remaining stack
events up to the window bound (the events API has no entity-id filter). For any
absence still unexplained, it performs a direct stack read; a `404` confirms
that the stack was deleted, including when its delete landed in an earlier sync
because the feed is not in commit order. A successful direct read leaves the
absence unexplained, so it raises `StackRowReadIncomplete` and truncates the
stream with the cursor preserved; another direct-read failure also truncates
the stream rather than excusing the row. Read lag resolves on the next sync.
Undecodable-id rows are excluded from the guard — the read returned them; they
are degraded deliberately.

The asset converter maps a member's `stack_id` to the Immich `stackId` through
the shared `immich_stack_id` helper (`gumnut_id_conversion.py`); the upload-ready
WebSocket payload uses the same helper, so both surfaces treat membership
identically.

Stack references stay out of `FK_REFERENCES`. When a stack dissolves, the
Gumnut API emits an `asset_updated` clearing each member's `stack_id` before
`stack_deleted`. Those upserts run in phase 1 and the stack delete in phase 2,
so members stop referencing the stack before the client removes it. Asset
updates use the payload's event-time `stack_id`, preventing a later move outside
the sync window from leaking into the current event. Unlike the face→person and
album→cover payload references, stacks are deliberately not verified per event:
with no client-side `stackId` FK, a transient reference to an already-removed
stack is a self-healing timeline-visibility gap, not an unacknowledgeable insert
failure.

`PartnerStacksV1` remains a no-op because partner sharing is unsupported.

## Adding a New Sync Type Version

When the same gumnut entity type maps to multiple Immich sync versions (e.g., AssetFacesV2 alongside V1), update these files in coordination:

1. `stream.py`: Add the V2 entry to `_SYNC_TYPE_ORDER` (after V1, same gumnut entity type) and a `_V1_SUPERSEDED_BY_V2` entry so V1 is skipped when V2 is also requested (prevents duplicate events). **Extend every `sync_entity_type`-gated payload override in `_stream_entity_type` to also match the V2 type** — the "Face person_id Handling" and "Album Cover Handling" overlays above are gated on the sync entity type, and a V2 type left off silently drops that FK-safety guarantee on the v3 client path (the client streams that entity exclusively as V2).
2. `fk_integrity.py`: Add V2 to the entity's list in `_GUMNUT_TYPE_TO_SYNC_TYPES` so FK checkpoint lookups match regardless of which version was synced — a client that checkpointed under the V2 type otherwise misses the "synced in a prior cycle" skip and logs spurious FK warnings.
3. `converters.py`: Write a V2 converter function alongside the V1 one.
4. `events.py`: Update the converter dispatch in `convert_entity_to_sync_event` to select V1 vs V2 converter based on `sync_entity_type`.
5. `test_sync_stream_ordering.py`: Verify the consistency test handles one-to-many gumnut-type-to-sync-type mappings.

The invariant tests in `test_sync_v2.py` assert that every V2 type in `_SYNC_TYPE_ORDER` is wired into the event dispatch (step 4), the FK checkpoint map (step 2), and — for albums — the cover override (step 1), so a half-wired addition fails a test instead of shipping. Extend them when adding a version.

## No-Op Request Types

Immich sync types that are accepted but have no Gumnut equivalent (e.g.,
`AssetEditsV1`, which has no edit-history sync source) go in
`_NOOP_REQUEST_TYPES` in `stream.py`. This sync no-op is separate from the
implemented HTTP edit routes described in
[`asset-and-media-handling.md`](../references/asset-and-media-handling.md):
the routes support reading and writing the current Immich edit recipe, but
edit-history events are not streamed through this endpoint. Do not just add
such types to `_SUPPORTED_REQUEST_TYPES` without `_SYNC_TYPE_ORDER` — that
silently drops them.

The v3 mobile client requests these types on every sync, so keep unsupported
ones in `_NOOP_REQUEST_TYPES` to avoid repeated warnings. `UserMetadataV1` is
handled specially outside `_SYNC_TYPE_ORDER` and must not be listed as a no-op.

## Contract with the Gumnut API

The adapter depends on the events API response shape (`EventsResponse`). Fields like `payload` are typed in the SDK (v0.52.0+) and accessed directly. For backward compatibility with old events that predate a field, check for `None` before use.

### Current-state verification is not snapshot-aware

Payload FK verification reads current Gumnut state while the events query is
bounded at the first read's `as_of`. If a referenced person or asset is deleted
during the cycle, verification can null the reference one cycle before the
bounded delete event arrives. The client converges on the same final state; the
temporary early null is preferred to emitting an FK that can permanently wedge
sync. Snapshot-aware verification would require an event-timeline lookup or
entity reads bounded by `as_of`, which only bounds the events feed.

### Interrupted delete phase

If the stream ends after upserts but before buffered deletes, an older delete
cursor can fall behind an acknowledged upsert cursor and the client may retain a
stale entity until a full sync. The implementation logs this condition. A
two-pass checkpoint model could remove the tradeoff, but the current design
prioritizes preventing unacknowledgeable FK failures.

## Debugging Immich Mobile Logs

Immich mobile app logs contain Immich UUIDs, not Gumnut IDs. When debugging sync issues from mobile logs, use `routers/utils/gumnut_id_conversion.py` to convert UUIDs to Gumnut IDs (e.g., `face_`, `person_`, `asset_` prefixed) before looking up entities in production via API or MCP tools.
