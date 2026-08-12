---
title: "Session and Checkpoint Storage Reference"
last-updated: 2026-08-11
---

# Session and Checkpoint Storage Reference

This reference defines the Redis records owned by `SessionStore` and `CheckpointStore`. For OAuth, request authentication, refresh, sync, acknowledgement, and logout flow, read the [session and checkpoint architecture](../architecture/session-checkpoint-implementation.md).

The adapter uses core Redis data structures only; it does not require RedisJSON or RediSearch.

## Session records

`services/session_store.py` owns the complete field set and serialization.

| Key | Redis type | Purpose |
|-----|------------|---------|
| `session:{uuid}` | Hash | One adapter session, including the encrypted Gumnut JWT and client metadata |
| `user:{user_id}:sessions` | Set | Session UUIDs associated with one user |
| `sessions:by_updated_at` | Sorted set | Explicit stale-session maintenance ordered by session activity |

The stable session UUID is the client-facing Immich access token. It is independent of the backend JWT, so a JWT refresh can update encrypted server-side custody without changing the client token or checkpoint namespace.

Representative session fields are `user_id`, `library_id`, `stored_jwt`, `device_type`, `device_os`, `app_version`, `created_at`, `updated_at`, and `is_pending_sync_reset`. Treat `services/session_store.py::Session` as the field authority rather than copying defaults or client-version examples here.

Session hashes may have a TTL. When a TTL is configured, the checkpoint hash receives the same TTL. Redis expiry does not remove set/sorted-set index entries; normal user-session reads lazily prune orphans, and `SessionStore.cleanup_stale_sessions` is an explicit maintenance operation rather than a background scheduler.

## Checkpoint records

| Key | Redis type | Field | Value |
|-----|------------|-------|-------|
| `session:{uuid}:checkpoints` | Hash | Generated `SyncEntityType` value | `{updated_at}|{cursor}` |

A synthetic record looks like:

```text
session:00000000-0000-4000-8000-000000000001:checkpoints
  AssetV2 = 2026-08-11T20:15:30.123456+00:00|cursor-example-001
```

### First component: `updated_at`

`updated_at` is when `CheckpointStore` wrote the Redis value. It is inspection metadata; it is not the sync position and it does not drive session activity cleanup.

### Second component: `cursor`

`cursor` is the opaque resume position acknowledged for that Immich sync entity type. Event-backed streams use Gumnut API event cursors. User-derived streams use a cursor derived from the current user record. Sync resumes from this component, not from `updated_at`.

`Checkpoint.to_redis_value` and `Checkpoint.from_redis_value` in `services/checkpoint_store.py` are the serialization authority.

## Compatibility and corruption behavior

The current reader accepts exactly two pipe-delimited components and parses the first as an ISO timestamp. It does not maintain a compatibility parser for older timestamp-only or differently ordered values.

A malformed checkpoint value is logged and skipped by `CheckpointStore.get` or `get_all`. The affected entity type therefore has no usable checkpoint and re-syncs from its normal starting position. This fail-open-to-resync behavior is safer than resuming from an ambiguous cursor.

The ack wire format is distinct from Redis storage:

| Boundary | Format |
|----------|--------|
| Immich client ↔ adapter | `{SyncEntityType}|{cursor}|` |
| Adapter checkpoint hash | field = `SyncEntityType`; value = `{updated_at}|{cursor}` |

See the [sync wire reference](immich-sync-communication.md#acknowledgements) for parsing and reset behavior.
