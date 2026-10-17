---
title: "Sync Stream Architecture"
last-updated: 2026-10-16
---

# Sync Stream Architecture

The sync stream consumes events from the Gumnut API and converts them to
Immich sync format. `routers/api/sync/stream.py` is its entry point and owns
the current sync-type order, event classification and tunables; this page owns
the contracts they implement.

## Two-Phase Ordering

The stream yields upserts one sync type at a time in foreign-key (FK)
dependency order, parents first. Delete events are buffered for a final pass in
the reverse order, children first. Two retractions are emitted inline during
the upsert phase: V1 face-geometry gating can emit `AssetFaceDeleteV1`, and an
asset absent from the session library emits `AssetDeleteV1`. Keeping these
retractions in feed order lets a later event in the same pass restore the row.
See the [sync stream event ordering design
doc](../design-docs/sync-stream-event-ordering.md) for the full design rationale
and history.

Failure is not isolated per pass: an unhandled fetch error ends the stream
without `SyncCompleteV1`, dropping every later pass. Degrade a row to a skip
only when skipping cannot strand dependent rows; for stacks, that applies to an
undecodable ID, not an empty member read. Retriable failures must propagate so
the event cursor remains unacknowledged.

## Event Classification

Each event is either a delete, built from the event itself, or an upsert, which
reads the entity's current state from the Gumnut API. Any event type the adapter
does not know as a delete is an upsert, so an unfamiliar type is a re-read. That
is how `asset_moved_out` and `asset_moved_in` are handled.

## Repeated Events for One Entity

A first sync has no checkpoint, so it replays the library's whole event history,
and one entity can appear in it once per change ever made to it. For an entity
type whose upsert is built from current state alone, every one of those events
would produce the same row. Such a pass emits an entity at its first event and
skips the rest, without reading the entity again. The stream summary logs the
skipped count as `repeat_event_skips`.

An entity such a pass could not read is not read or reported again either,
because the feed holds only committed changes and the row is therefore deleted.
Two types are exceptions. A stack absent from the read counts as deleted only
once the guard in [Stacks](#stacks-stacksv1) has explained or confirmed the
absence. Metadata is re-read: its read also misses an asset that is trashed or
outside the session library, which can change mid-pass.

A skipped event carries no ack. So the client's checkpoint still moves past a
run of repeats, the pass emits a `SyncAckV1` line — a no-op the client echoes
back — acked under the pass's own sync type at the last skipped cursor: once at
the end of the pass, and at a page boundary once a set number of repeats have
gone unacked. The client sends one ack request per run of same-type lines, so
these lines are deliberately sparse. Only repeats of an emitted entity move the
checkpoint this way; events for an absent entity never emitted a row and still
do not.

Face, asset and album upserts are excluded because they take event-time values from the event payload (see the handling sections below), so two events for one entity can produce different rows. The album-user row derived from album events is current-state too, but its pass is left as it is: albums have few events.

Deduplication must not make a pass wait on a type's whole history. A client syncs incrementally and is eventually consistent: it reads a few pages, reconciles those entities, acks, and resumes later. So deduplicating a type must not depend on reading or holding its whole event history before any upsert is emitted — for example, to find each entity's last event. Emitting the payload-dependent types once per sync would therefore need the Gumnut API to identify an entity's last event; the adapter cannot know it from the pages read so far.

## Deletion Events

A delete event names the deleted entity by ID, which the adapter converts to its
Immich UUID. For junction table deletions (e.g., `album_asset_removed`), the
event's `payload` field carries the foreign keys since the record is
hard-deleted. The feed is not in commit order, so a later re-add can sort ahead
of the removal; the adapter re-reads each removed pair and upserts the current
membership instead when it exists.

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
- **Keeping a stack in its library.** The API never moves a stack. A moved
  asset leaves its stack and arrives unstacked, and the destination may stack
  it later; each is an ordinary stack and asset event in its own library's
  feed. So the by-ID read that confirms an absent stack row (see
  [Stacks](#stacks-stacksv1)) is correct without being scoped to a library.

## Gating Rows Is a State Transition, Never an Omission

A pass that must hide rows from clients (e.g., the face-geometry gate — see
`docs/references/asset-and-media-handling.md`) cannot just skip the upsert: a
client that synced the row before the condition arose keeps its stale copy
forever, and the cursor advances past the discarded event. Emit the hidden
state instead — a visibility flag where the sync type has one, a retraction
delete where it doesn't.

The face pass decides the gate from each face's owning asset and, for the rest
of the pass, does not read again an asset it found exposable (see
`routers/api/sync/entity_fetch.py`). An edit made after that read is therefore
not seen by the pass; the row converges as it does for an edit made after the
pass ends (see `docs/references/asset-edits-and-downloads.md`).

## Face person_id Handling

`face_created` events have person_id nulled out (face detection never assigns a
person). `face_updated` events use the causally-consistent person_id from the
event payload instead of current entity state. Payload person_ids are verified
against current Gumnut state before the faces that carry them are emitted; a
person the Gumnut API no longer finds is nulled out on the outgoing event,
regardless of whether a `PersonV1` checkpoint exists. This prevents stale
payload references (from clustering runs that predate a person's deletion) from
leaking across sync cycles and causing FK violations on the client.

## User Preferences (minimumFaces)

Gumnut has no per-user preferences, but the v3 client reads `people.minimumFaces`
from the `UserMetadataV1` stream to decide which people appear in the People tab,
defaulting to **3** when absent — which would hide Gumnut clusters of 1–2 faces.
The adapter synthesizes a single `UserMetadataV1` *preferences* row with
`minimumFaces=1` and ratings enabled (all other fields mirror the client's
defaults). It's emitted right after `UserV1` (the `userId` FK parent) and keyed
off the **same cursor as `UserV1`** (the user's `updated_at`) — the payload is
derived purely from the user, so it re-syncs exactly when the user record changes and
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

To cover this, `AlbumUsersV1` is its own sync type, read from the same Gumnut
album events as `AlbumsV1/V2` and streamed **after** the album (FK parent) and
after the owner `UserV1`. Each album fans out to two sync entities — the album
(`AlbumV1/V2`) and its owner link (`AlbumUserV1`) — both derived from the same
`AlbumResponse`. Gumnut is single-user with no album sharing, so every album has
exactly one album-user: the owner (`role=owner`).

The album-user pass streams upserts only. Album-user *deletes* are owned by the
album pass, which the v3 client always requests alongside it: an `album_deleted`
event emits `AlbumDeleteV1`, and the client's `remoteAlbumUserEntity.albumId` FK
cascades on album deletion — re-emitting the delete from the album-user pass
would duplicate `AlbumDeleteV1`. No `AlbumUserDeleteV1` is emitted (Gumnut has
no unshare operation).

## Stacks (StacksV1)

`StacksV1` uses the event cursor and streams before assets. The mobile timeline
hides an asset whose `stackId` names an unknown stack, so a `stack_created` or
`stack_updated` event must emit `StackV1` before its member assets. The client
has no stack foreign key (the column is indexed only); this is a visibility
constraint. Deletes use the inverse order, with `StackDeleteV1` after asset
deletes.

`StackV1.primaryAssetId` is required but an unpinned Gumnut stack has no primary
on its row. Sync therefore reads each stack together with its live member IDs,
applies the shared effective-primary rule, and carries only the resulting UUID
into the converter. A stack with a live member and no trashed pin costs no
further request, which keeps a first sync inside the upstream rate limit. Only a
pin outside the live members or a stack with no live member needs a
concurrency-bounded member read. A row without member IDs or an empty all-state
member read is retryable, because the row still exists; propagating the error
truncates the stream before its cursor is acked.

A stack row **absent from the bulk stack read** is likewise retriable —
skipping it would advance the cursor past the stack while the asset pass still
stamps `stackId` on its members, hiding the burst on the mobile timeline. The
guard first excuses exactly the ids with a `stack_deleted` event in the window:
first the current events page, then a look-ahead scan of the remaining stack
events up to the window bound (the events API has no entity-id filter). For any
absence still unexplained, it performs a direct stack read; a `404` confirms
that the stack was deleted, including when its delete landed in an earlier sync
because the feed is not in commit order. A successful direct read leaves the
absence unexplained, so the pass fails and truncates the stream with the cursor
preserved; another direct-read failure also truncates the stream rather than
excusing the row. Read lag resolves on the next sync.
Undecodable-id rows are excluded from the guard — the read returned them; they
are degraded deliberately.

The asset converter maps a member's `stack_id` to the Immich `stackId` through
the shared `immich_stack_id` helper (`gumnut_id_conversion.py`); the upload-ready
WebSocket payload uses the same helper, so both surfaces treat membership
identically.

When a stack dissolves, the Gumnut API emits an `asset_updated` clearing each
member's `stack_id` before `stack_deleted`. Those upserts run in phase 1 and the
stack delete in phase 2, so members stop referencing the stack before the
client removes it. Asset updates use the payload's event-time `stack_id`,
preventing a later move outside the sync window from leaking into the current
event. Unlike the face→person and album→cover payload references, stack
references are deliberately not FK-checked or verified per event: with no
client-side `stackId` FK, a transient reference to an already-removed stack is
a self-healing timeline-visibility gap, not an unacknowledgeable insert
failure.

`PartnerStacksV1` remains a no-op because partner sharing is unsupported.

## Adding a New Sync Type Version

When one Gumnut entity type gains a second Immich sync version (e.g.,
`AssetFacesV2` alongside V1), every place that keys on the sync type must learn
the new one. A half-wired version streams without error but loses a guarantee:

- **Stream order.** The new version streams in its entity type's existing
  position, and a client that requests both versions receives only the newer
  one, so no event is emitted twice.
- **Payload overrides.** Every override gated on the sync entity type — the
  face person_id and album cover handling above — must match the new type too.
  The v3 client streams such an entity only as the newer version, so a version
  left off silently loses that FK-safety guarantee.
- **FK checkpoint lookups** (`fk_integrity.py`). A checkpoint under either
  version counts as "synced in a prior cycle"; otherwise a client checkpointed
  under the new type logs spurious FK warnings.
- **Conversion** (`converters.py`, dispatched from `events.py`). Each version
  has its own converter, selected by the sync entity type.

The invariant tests in `tests/unit/api/sync/test_sync_v2.py` and
`test_sync_stream_ordering.py` catch some half-wired additions, not all of
them, so check each point above by hand and extend the tests when adding a
version.

## No-Op Request Types

Immich sync types that are accepted but have no Gumnut equivalent (e.g.,
`AssetEditsV1`, which has no edit-history sync source) are declared as no-ops in
`stream.py`: the stream emits nothing for them and logs them as intentional
rather than unsupported. This sync no-op is separate from the implemented HTTP
edit routes described in
[`asset-and-media-handling.md`](../references/asset-and-media-handling.md): the
routes support reading and writing the current Immich edit recipe, but
edit-history events are not streamed through this endpoint. A request type is
supported only by streaming it or declaring it a no-op; marking one supported
without either silently drops it.

The v3 mobile client requests these types on every sync, so an undeclared one is
logged as unsupported each cycle. `UserMetadataV1` is synthesized from the user
(see [User Preferences](#user-preferences-minimumfaces)) and must not be
declared a no-op.

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
