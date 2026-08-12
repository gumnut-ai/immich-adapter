---
title: "Sync Stream Event Ordering"
status: deprecated
superseded-by: ../architecture/sync-stream-architecture.md
created: 2026-03-13
last-updated: 2026-08-11
---

# Sync Stream Event Ordering

> **Deprecated —** This record explains why the adapter adopted two-phase streaming and current-state FK verification. The live ordering, hydration, checkpoint, and failure contracts are in [Sync Stream Architecture](../architecture/sync-stream-architecture.md).

## Problem

The Immich mobile database enforces foreign keys while processing sync records in entity-type batches. The adapter consumes a chronological Gumnut event stream, but grouping that stream by Immich entity type can reorder causally related changes.

A representative failure was:

1. a person existed and a face referenced it;
2. the person was later deleted and the face was reassigned;
3. the person batch applied the deletion before the face batch applied its older reference;
4. SQLite rejected the face row;
5. the client withheld the face acknowledgement and retried the same failing window indefinitely.

The key mismatch is architectural: upstream Immich sync reads current table state bounded by update IDs, while the adapter replays events and hydrates entities from another API.

## Decision

Split generation into two phases:

- parent-first upserts across entity families;
- child-first deletes after every upsert pass.

Use causally consistent event payload references where current hydration could leak a later assignment into an older event. Before emitting those references, verify that the referenced entity still exists in current Gumnut state and null references that are confirmed missing.

This favors a recoverable stale/cosmetic interval over a permanently stuck client sync.

## Evolution of the solution

### Created faces

A `face_created` event can be hydrated after clustering has already assigned a person outside the event's sync window. The adapter therefore does not attach the later assignment to the created-face event.

### Updated-face payloads

For `face_updated`, the event payload carries the assignment at event time. Using it avoids leaking a later clustering result into an earlier window.

### Two-phase deletion order

Payload consistency alone did not protect a reference whose parent was deleted later in the same client cycle. Buffering deletes until all upserts completed established the required FK order.

### Missing-reference handling

Only considering 404s encountered during normal entity hydration missed parents deleted before the current window. The adapter therefore extracts payload FK references and verifies unresolved IDs in bounded bulk reads. A confirmed missing person or album-cover asset overrides assumptions based on an older checkpoint.

This sequence matters because each partial fix addressed one time axis—event time, sync-window time, processing order, or current existence—without covering the others.

## Why upstream Immich can order differently

Upstream reads current tables, where database cascades have already removed invalid references. Update-ID bounds also move a changed child into the same or a later window. It can safely emit deletes before upserts within a type because the upsert payload already reflects current referential state.

The adapter cannot copy that ordering while it relies on Gumnut events plus current entity hydration. A future direct-entity-query design could converge on upstream behavior, but would require new backend filters and a dual strategy for hard deletes.

## Tradeoffs

### Interrupted two-phase stream

If a stream ends after upserts but before buffered deletes, a client may retain a stale deleted entity until a later full sync. Avoiding this would require a checkpoint model that represents both passes. The chosen behavior avoids acknowledging data that failed to hydrate and prevents the more severe permanent FK retry loop.

### Current-state verification vs. snapshot

Reference verification reads current Gumnut state while the event query is bounded at sync start. A parent deleted during the cycle can therefore be nulled slightly earlier than the bounded event snapshot implies; its delete arrives next cycle. The end state converges, and the temporary discrepancy is safer than emitting an invalid FK.

Snapshot-aware verification would require event-timeline queries or an `as_of` read contract across the API boundary. That complexity was not warranted for a cosmetic one-cycle difference.

### Verification cost

Unresolved payload references can require one bounded bulk read per event batch and referenced type. This was accepted because stuck mobile sync has higher impact. The implementation logs aggregate degradation rather than one record per entity.

## Outcome

Closed on **2026-08-11** after reverification against the default branch:

- `routers/api/sync/stream.py` owns parent-first upserts, buffered child-first deletes, and checkpoint-preserving failure behavior;
- `routers/api/sync/fk_integrity.py` owns payload-reference extraction, bounded verification, and missing-reference nulling;
- `routers/api/sync/events.py` owns delete/event conversion and ack construction;
- regression tests cover ordering, V2 mappings, payload overrides, and FK consistency.

The approach shipped and accumulated the refinements above. Current operational guidance was extracted to [Sync Stream Architecture](../architecture/sync-stream-architecture.md); this document remains the rationale and alternatives record.
