---
title: "Immich Sync Wire Reference"
last-updated: 2026-08-11
---

# Immich Sync Wire Reference

This reference describes the Immich v3 sync shapes the adapter accepts and emits. For ordering, entity hydration, foreign-key protection, and failure behavior, read the [sync stream architecture](../architecture/sync-stream-architecture.md). The generated `SyncRequestType`, `SyncEntityType`, and `Sync*V*` models in `routers/immich_models.py` are the schema authority.

## Stream request

`POST /api/sync/stream` accepts `SyncStreamDto`:

```json
{
  "reset": false,
  "types": ["AuthUsersV1", "AssetsV2", "AlbumsV2", "AlbumUsersV1"]
}
```

- `types` contains generated `SyncRequestType` values. Do not copy an enum count into documentation; regeneration can add values without changing the protocol.
- `reset=true` clears the session's stored checkpoints before streaming.
- The adapter implements some request types, synthesizes user metadata, and explicitly accepts unsupported feature families as no-ops. `_SYNC_TYPE_ORDER`, `_NOOP_REQUEST_TYPES`, and `_SUPPORTED_REQUEST_TYPES` in `routers/api/sync/stream.py` own that classification.
- When both V1 and its supported V2 successor are requested, `_V1_SUPERSEDED_BY_V2` suppresses the duplicate V1 pass.

## Stream response

The response is newline-delimited JSON. Each line has the same envelope:

```json
{"type":"AssetV2","data":{"id":"00000000-0000-4000-8000-000000000001","ownerId":"00000000-0000-4000-8000-000000000002","originalFileName":"example.jpg"},"ack":"AssetV2|cursor-example-001|"}
```

The abbreviated `data` above is illustrative, not a complete payload. Use the generated model named by `type` for required fields and constraints. Synthetic UUIDs, filenames, and cursors are deliberate: captured production payloads do not belong in this public repository.

`routers/api/sync/events.py::make_sync_event` owns the envelope and JSON serialization. Entity converters in `routers/api/sync/converters.py` own the adapter's payload construction.

## Request and entity types

A request type selects a stream. A stream can emit more than one entity type: for example, an album request can produce an album upsert and an album delete, while `AlbumUsersV1` derives an owner-link row from the same album events. Versioned request types can share a source entity but use different payload models.

Use these sources instead of a copied exhaustive mapping:

- `routers/immich_models.py::SyncRequestType` — accepted request enum.
- `routers/immich_models.py::SyncEntityType` — ack and response entity enum.
- `routers/api/sync/stream.py::_SYNC_TYPE_ORDER` — implemented request-to-entity mapping and FK-safe order.
- `routers/api/sync/stream.py::_NOOP_REQUEST_TYPES` — accepted request types with no Gumnut equivalent.
- `routers/api/sync/events.py::convert_entity_to_sync_event` — entity-to-generated-model dispatch.
- `docs/architecture/sync-stream-architecture.md` — why the order and derived streams exist.

### V1 and V2 payload differences

The generated models remain authoritative. The adapter currently has three implemented V2 families:

- `SyncAssetV2` carries duration as integer milliseconds; V1 uses the interval-string form.
- `SyncAlbumV2` omits `ownerId`; the separate `AlbumUserV1` stream supplies the owner relationship required by the v3 client.
- `SyncAssetFaceV2` adds face deletion/visibility fields.

Unsupported v3 families such as partner sharing and OCR are explicit no-ops rather than partial payloads.

## Acknowledgements

Every emitted line includes:

```text
{SyncEntityType}|{cursor}|
```

The trailing pipe is reserved for compatibility with Immich's wire format. The cursor is opaque and must not contain `|`; `routers/api/sync/events.py::to_ack_string` logs that invalid condition. Most event-backed records carry the Gumnut API events cursor. User-derived rows use the user's `updated_at` value, and the completion record uses an adapter-owned completion cursor.

The client acknowledges processed positions with `POST /api/sync/ack`:

```json
{"acks":["AssetV2|cursor-example-001|","AlbumV2|cursor-example-002|"]}
```

`routers/api/sync/routes.py::_parse_ack` validates the entity enum, skips malformed or empty-cursor values, and stores the last value for duplicate entity types in one request. `GET /api/sync/ack` reconstructs the same wire strings from stored checkpoints. A `SyncResetV1` acknowledgement clears every checkpoint and the pending-reset flag; other acknowledgements in that request are ignored.

## Completion and reset

- `SyncCompleteV1` terminates every successfully generated stream, including a stream with no entity changes.
- A session with `is_pending_sync_reset` receives `SyncResetV1` and no normal entity stream.
- An unhandled hydration or transport failure ends the generator without `SyncCompleteV1`. This is intentional: the client must not acknowledge a position past data that failed to stream.
