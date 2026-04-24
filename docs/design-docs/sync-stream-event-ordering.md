---
title: "Sync Stream Event Ordering"
status: active
created: 2026-03-13
last-updated: 2026-04-16
---

# Sync Stream Event Ordering

## Problem

The Immich mobile app enforces SQLite FK constraints when inserting sync events. The sync stream groups events by entity type (e.g., all person events before all face events). Within each type, events are yielded chronologically, including both upserts and deletes.

When a person is deleted and their faces are reassigned:

1. `person_deleted` is yielded (within the person entity type)
2. `face_updated` referencing the old person is yielded later (within the face entity type)
3. The mobile app processes all person events (including the delete) before any face events
4. The face insert fails with `SqliteException(787): FOREIGN KEY constraint failed`
5. The face batch fails atomically, no ack is sent, and sync retries the same events forever

## Solution: Two-Phase Streaming

The sync stream is split into two phases:

- **Phase 1 (Upserts):** Stream creates/updates for all entity types in FK dependency order (assets → albums → album_assets → metadata → persons → faces)
- **Phase 2 (Deletes):** Stream deletes for all entity types in reverse FK dependency order (faces → album_assets → persons → albums → assets)

This ensures:
- Parents exist before children reference them (upserts in FK order)
- Children are cleaned up before parents are removed (deletes in reverse FK order)

### Event Flow Example

Given events: person_created (cursor 10), face_updated with person_id=X (cursor 20), person_deleted X (cursor 30), face_updated with person_id=null (cursor 40)

**Before (broken):** PersonV1(10), PersonDeleteV1(30), AssetFaceV1(20), AssetFaceV1(40) → FK violation at event 3

**After (fixed):** PersonV1(10), AssetFaceV1(20), AssetFaceV1(40), PersonDeleteV1(30) → all FK constraints satisfied

## History of Face/Person FK Issues

This is the third iteration of face/person FK constraint fixes, all stemming from the same root cause: the adapter sends face events with person_ids that don't exist on the client at processing time.

### Fix 1: Null person_id on face_created (PR #74)

Face detection creates faces without a person. Clustering assigns a person later. When the adapter fetches current entity state for a `face_created` event, the face may already have a person_id from clustering — but the corresponding `person_created` event may fall outside the sync window (`created_at_lt`). Fix: null out person_id on `face_created` events.

### Fix 2: Payload override for face_updated (PR #78)

For `face_updated` events, use the `person_id` from the event payload instead of the entity's current state. The payload records the causally-consistent person_id at event time, avoiding references to persons assigned by later clustering runs that fall outside the sync window.

### Fix 3: Upserts before deletes (PR #85)

The payload fix solved the time-window problem but introduced a deletion-ordering problem. The payload's person_id was valid at event time, but the person may have been deleted by the time the mobile app processes the face event. Fix: buffer delete events and yield them after all upserts.

### Fix 4: Null payload references to deleted entities (PR #88)

The two-phase fix handles ordering within a sync cycle, but the payload override can still reference an entity that was deleted after the event was recorded. On a fresh sync (no prior data), the deleted entity returns 404 during fetch and is never streamed. The face/album arrives with a reference to a non-existent entity, causing an FK violation.

Fix: after applying a payload override, check if the referenced entity ID is in the set of IDs that returned 404 during fetch (`stats.not_found_ids`). If so, null it out. The guard is skipped when the referenced entity type has a checkpoint, since the entity may exist on the client from a prior sync cycle. Applies to both `face_updated` (person_id) and `album_updated` (album_cover_asset_id).

### Fix 5: Verify payload references against production (GUM-545)

Fix 4 only populated `not_found_ids` for entities deleted within the current sync window — if the referenced entity was deleted before the window started, the adapter never tried to fetch it and had no 404 to record. Combined with Fix 4's checkpoint-skip guard ("the entity may exist on the client from a prior sync cycle"), this leaked stale payload references across cycles:

1. Prior cycle: client acked `person_created P1` and, later, `person_deleted P1`. Client no longer has P1 locally, but `PersonV1` checkpoint has advanced past both events.
2. Current cycle: `face_updated` with payload `{"person_id": P1}` sits in the face window; no person events remain for the face's clustering runs outside this window.
3. Fix 4's guard skipped the null-out because `PersonV1` was in `checkpoint_map`. The payload person_id leaked through.
4. Mobile client tried to insert the face referencing P1 — SQLite FK violation (`asset_face_entity` → `person`), sync stuck.

Concrete production timeline for the face that surfaced GUM-545 (event IDs from photos-api):

- 79099 `person_created P1`
- 79100 `face_updated` payload person_id=P1
- 79117 `face_updated` payload person_id=P_mid (clustering reassigned)
- 79161 `person_deleted P1`
- 79183 `face_updated` payload person_id=P_final (current state)

Any client whose `AssetFaceV1` checkpoint lags at cursor < 79100 while `PersonV1` is past 79161 will receive the stale event 79100 and FK-fail.

Fix: scan each event batch for payload-referenced FK IDs before streaming (see `extract_payload_fk_refs` in `fk_integrity.py`) and batch-fetch any IDs that are neither already streamed nor already known-404. Missing IDs from that fetch populate `stats.not_found_ids` — which is now authoritative about production existence regardless of the client's checkpoint state. The checkpoint-skip guard in `null_deleted_fk_references` is removed: a confirmed 404 overrides any assumption about what the client may still hold, because the client necessarily processed the corresponding `*_deleted` event in the cycle that advanced its checkpoint. Applies to both `face_updated` (person_id) and `album_updated` (album_cover_asset_id).

Cost: one extra bulk list call per event batch per referenced type for IDs not yet streamed or already-known 404 in this cycle. Verified-present IDs are not memoized across batches, so a live entity referenced by events spanning multiple `EVENTS_PAGE_SIZE=200` batches (e.g., large re-clustering runs) is re-fetched once per batch. In practice this collapses to one call per cycle per referenced type for most sessions (single batch, single call).

## How the Real Immich Server Avoids This

The real Immich server (`server/src/services/sync.service.ts`) has a fundamentally different architecture:

- **Current-state queries, not event replay.** Upserts query the main table. If a person was deleted, the DB cascade (`ON DELETE SET NULL`) has already nullified `face.person_id`. Face upserts never reference deleted persons.
- **`updateId` gating.** Every entity has an `updateId` field (UUID v7, timestamp-based). When clustering assigns a person to a face, the face's `updateId` changes. The sync window uses `updateId < nowId` — if the person assignment falls outside the window, the face's update also falls outside. Both are excluded together.
- **Delete-first within each type.** The real server sends deletes before upserts within each entity type (opposite of our approach). This works because upserts contain current state with deleted references already nullified.

Our adapter can't replicate this because we consume events from photos-api (fixed timestamps, separate from entity state) rather than querying entities directly.

## Checkpoint Behavior

Checkpoints are managed by the mobile client, not the adapter:

1. Each sync event includes an `ack` string containing the event's cursor
2. The mobile client acks the last event in each SyncEntityType batch
3. On next sync, the adapter receives those acks and uses them as `after_cursor` for the events API
4. The adapter is stateless — it passes cursors through

Delete events use separate SyncEntityTypes (e.g., `PersonDeleteV1` vs `PersonV1`), so delete checkpoints don't interfere with upsert checkpoints.

## Known Tradeoff: Interrupted Stream

If the stream is interrupted between phase 1 (upserts) and phase 2 (deletes), delete events whose cursors fall before the acked upsert cursor may be lost on resume. The client retains stale entities until the next full sync.

This is an acceptable tradeoff: permanently stuck sync (the bug) is far worse than occasionally stale deleted entities (recoverable).

## Known Tradeoff: Fix 5 Verification Uses Current State, Not Snapshot

The Fix 5 verification fetch (`people.list` / `assets.list` for payload-referenced IDs) reads the live production state of those entities, while the surrounding event stream is bounded by `created_at_lt=sync_started_at` (a point-in-time snapshot). If a person or asset is deleted *during* a sync cycle — after `sync_started_at` but before the face/album phase runs the verification — the verification will see a 404 and null the reference even though it was valid at the snapshot. The corresponding `*_deleted` event falls outside the current cycle's window (its cursor > `sync_started_at`), so it arrives in the next cycle instead.

End-state impact: none. The client still receives the delete event in the next cycle; the face/album converges to the same final state (orphan reference, or cascaded to NULL). The only visible difference is a brief interval where the client shows the reference already nulled instead of "valid but about to be deleted." For the concrete GUM-545 FK-violation failure mode this is strictly safer (no stuck sync), and the cosmetic inconsistency resolves within one sync cycle.

Making the verification snapshot-aware would require either an event-timeline check (query for `*_deleted` events with cursor ≤ `sync_started_at`) or a cross-repo `as_of` parameter on the list endpoints. Both add meaningful complexity for a cosmetic win; not implemented.

## Future Alternative: Direct Entity Queries

A potential long-term alternative is querying entity endpoints directly (like the real Immich server) rather than replaying events:

- Query `updated_after` on entity list endpoints for upserts (current state, no time-window issues)
- Still use events API for deletes (deleted entities don't appear in list queries)
- Would require photos-api changes and a dual checkpoint system
- Would eliminate the entire class of time-window and ordering bugs
