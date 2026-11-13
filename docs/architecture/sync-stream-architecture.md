---
title: "Sync Stream Architecture"
last-updated: 2026-11-13
---

# Sync Stream Architecture

The sync stream consumes events from the Gumnut API and converts them to
Immich sync format. `routers/api/sync/stream.py` is its entry point and owns
event classification, orchestration and stream tunables.
`routers/api/sync/type_order.py` owns the shared request-to-entity mapping and
upsert dependency order; this page owns the contracts they implement.

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

An unhandled fetch error ends the stream without `SyncCompleteV1`. The failed
event and later passes are left for retry, subject to the
[buffered-delete limitation](#interrupted-delete-phase).

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

Face creation emits an unassigned face. A later clustering run may assign a
person whose creation is outside the sync window. Face updates use the
event-time person reference when the payload supplies it, subject to
[current-state verification](#current-state-verification-is-not-snapshot-aware).

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

Album updates use the event-time cover reference when the payload supplies it.
This keeps an asset added after the event from becoming its cover. The
[current-state verification](#current-state-verification-is-not-snapshot-aware)
checks the referenced asset within the session's library, including trash.

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

`StacksV1` uses the event cursor. The mobile timeline hides assets whose
`stackId` names an unknown stack, so stack upserts precede member upserts and
stack deletes follow asset deletes. Every emitted stack needs an effective
primary, even when Gumnut has no pinned primary. An incomplete member read
leaves the event for retry.

A missing stack row can strand otherwise valid member rows. The stream accepts
the absence when an in-window delete or a direct stack read returning `404`
establishes that the row is gone. An unexplained absence ends the stream so it
can retry before syncing member assets. Assets with undecodable stack IDs sync
as loose assets. [Stream orchestration](../../routers/api/sync/stream.py) owns
the missing-row guard, and
[entity hydration](../../routers/api/sync/entity_fetch.py) owns primary
selection and member reads.

Asset updates that carry stack membership in the event payload preserve that
event-time value. When a stack dissolves, member updates clear their references
before the client receives the stack delete. The client can accept an asset
with an unknown `stackId` because it has no stack FK. Subsequent events can
resolve the resulting timeline visibility gap.

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
- **FK checkpoint lookups** (`fk_integrity.py`). The lookup types are derived
  from the shared request-to-entity order, so every ordered version is
  recognized automatically. A checkpoint under either version counts as
  "synced in a prior cycle."
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

The stream uses parent rows already emitted or known absent in the current
stream and bulk-reads unresolved event-time person and cover references from
the Gumnut API. A parent absent from a completed read can clear the outgoing
reference even if the client has a checkpoint for the parent type. A failed
read ends the stream without completion, leaving the dependent event for
retry. [Stream orchestration](../../routers/api/sync/stream.py) and
[FK verification](../../routers/api/sync/fk_integrity.py) own these checks.

These reads use current state. The events feed alone is bounded by the first
read's `as_of`, so the client may see a cleared reference before it receives
the parent's delete event. Clearing it lets the client apply the dependent
row and continue syncing even if the parent is already gone locally.

### Interrupted delete phase

If the stream ends after upserts but before buffered deletes, an older delete
cursor can fall behind an acknowledged upsert cursor and the client may retain a
stale entity until a full sync. The implementation logs this condition. A
two-pass checkpoint model could remove the tradeoff, but the current design
prioritizes preventing unacknowledgeable FK failures.

## Debugging Immich Mobile Logs

Immich mobile app logs contain Immich UUIDs, not Gumnut IDs. When debugging sync issues from mobile logs, use `routers/utils/gumnut_id_conversion.py` to convert UUIDs to Gumnut IDs (e.g., `face_`, `person_`, `asset_` prefixed) before looking up entities in production via API or MCP tools.
