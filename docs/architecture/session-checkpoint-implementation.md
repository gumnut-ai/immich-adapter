---
title: "Session and Checkpoint Implementation in immich-adapter"
last-updated: 2026-06-11
---

# Session and Checkpoint Implementation in immich-adapter

## Summary

`immich-adapter` keeps two related pieces of Redis state:

- **Sessions** keyed by a stable UUID session token that Immich clients send back on later requests
- **Checkpoints** keyed by session and sync entity type so `/api/sync/stream` can resume from the last acknowledged cursor

The important distinction is that the client-facing token is **not** the backend JWT. The adapter encrypts the backend JWT, keeps it server-side, and updates it in place when the backend refreshes it. Sync resume is likewise **cursor-based**, not timestamp-hash-based: each entity type stores the last opaque Gumnut API events cursor that the client acknowledged.

## Redis data model

### Session keys

```text
session:{uuid}
  ├── user_id: "550e8400-e29b-41d4-a716-446655440000"
  ├── library_id: ""
  ├── stored_jwt: "<encrypted backend JWT>"
  ├── device_type: "iOS"
  ├── device_os: "iOS 18.5"
  ├── app_version: "1.136.0"
  ├── created_at: "2026-06-11T08:59:12.123456+00:00"
  ├── updated_at: "2026-06-11T09:04:55.654321+00:00"
  └── is_pending_sync_reset: "0"

user:{user_id}:sessions
  └── {session_uuid_1, session_uuid_2, ...}

sessions:by_updated_at
  └── {session_uuid -> updated_at_timestamp}
```

### Checkpoint keys

```text
session:{uuid}:checkpoints
  ├── AssetV1: "2026-06-11T09:04:55.654321+00:00|cursor_asset_123"
  ├── AlbumV1: "2026-06-11T09:04:55.654321+00:00|cursor_album_456"
  └── UserV1: "2026-06-11T09:04:55.654321+00:00|2026-06-11T08:58:00+00:00"
```

Each checkpoint value is stored as:

```text
{updated_at_iso}|{cursor}
```

- `updated_at_iso` is **when the adapter stored the checkpoint**
- `cursor` is the opaque resume token for that sync entity type

`updated_at_iso` is useful for inspection, but sync resume uses the cursor, not the timestamp. Session activity tracking and stale-session cleanup use `session.updated_at` and `sessions:by_updated_at`.

## Session flow

### OAuth login

`POST /api/oauth/callback` finishes the OAuth exchange:

1. The adapter parses the callback URL from the client.
2. It exchanges the code and state with the backend.
3. The backend returns a JWT plus user info.
4. `SessionStore.create()` generates a fresh UUID session token, encrypts the JWT, and stores the session in Redis.
5. The adapter returns that UUID to the client as `accessToken`, and for web clients also sets the auth cookies.

That means the session token a client stores is stable even when the backend later rotates the JWT. If the backend rejects a stale or replayed callback, the adapter lets that backend 400 response reach the client so the user can restart the login flow.

### Authenticated requests and JWT refresh

`AuthMiddleware` treats the UUID session token as the only client credential it needs. For each authenticated request it:

1. Extracts the session token from `Authorization: Bearer`, `x-immich-user-token`, or the `immich_access_token` cookie.
2. Loads `session:{uuid}` from Redis.
3. Decrypts `stored_jwt` and attaches it to `request.state` for downstream SDK calls.
4. Calls the route handler.
5. If the backend response includes `x-new-access-token`, updates `stored_jwt` in place and strips that header before the response reaches the client.

Clients therefore keep using the same UUID session token across backend JWT refresh cycles.

### Logout and session management

- `POST /api/auth/logout` deletes the current session by UUID token and clears auth cookies.
- `GET /api/sessions` returns a bare array of `SessionResponseDto` items for the current user.
- `DELETE /api/sessions` deletes every other session for the user while keeping the current one.
- `PUT /api/sessions/{id}` currently supports toggling `isPendingSyncReset`.
- `DELETE /api/sessions/{id}` deletes a specific session.
- `POST /api/sessions` and `POST /api/sessions/{id}/lock` are still 204 stubs.

Session records still carry `library_id` for compatibility and metadata, but the current sync implementation resumes from owner-scoped events cursors rather than `library_id` timestamp filters.

## Checkpoint model

Checkpoints are stored per session and per `SyncEntityType`. They are tied to the session lifecycle: deleting a session also deletes `session:{uuid}:checkpoints`.

The current implementation stores **opaque cursors**, not last-synced timestamps. That matters for two reasons:

1. `/api/sync/stream` resumes by passing the stored cursor back to the backend events API as `after_cursor`.
2. A checkpoint's `updated_at` field is bookkeeping only; it is not the resume position.

`GET /api/sync/ack` rebuilds ack payloads from stored checkpoints in this format:

```text
SyncEntityType|cursor|
```

Checkpoints without a cursor are skipped when reconstructing ack responses.

## Sync stream flow

### `POST /api/sync/stream`

The sync stream is driven by the backend events feed, with one checkpoint per entity type.

1. If `request.reset=true`, the adapter deletes all checkpoints for the session before streaming.
2. If the session has `is_pending_sync_reset=true`, the adapter returns a one-event stream containing `SyncResetV1|reset|` and stops.
3. Before the response starts, the route resolves `users.me()` so auth failures still surface as normal HTTP errors instead of being swallowed inside a streaming generator.
4. `AuthUsersV1` and `UsersV1` are emitted directly from the current user record, using `current_user.updated_at` (or the user id as a fallback) as their cursor.
5. Event-backed types resume from `checkpoint.cursor` using the backend events API with:
   - `after_cursor` for per-type resume
   - `created_at_lt=sync_started_at` for a bounded point-in-time window
6. Upserts stream first in foreign-key dependency order. Delete events are buffered and emitted afterward in reverse dependency order.
7. `AssetEditsV1` is accepted as a no-op request type, and `AssetFacesV1` is skipped when `AssetFacesV2` is also requested so the same face events are not streamed twice.
8. The stream finishes with `SyncCompleteV1|complete|`.

This two-phase ordering is the key behavior that keeps the mobile client's SQLite foreign keys consistent while still using a single events source.

## Sync ack flow

### `GET /api/sync/ack`

Returns the current checkpoint set for the session as `SyncAckDto[]`, rebuilding each ack as `SyncEntityType|cursor|`.

### `POST /api/sync/ack`

The adapter parses each ack string as `SyncEntityType|cursor|`.

- Invalid entity types return HTTP 400.
- Malformed strings and empty-cursor acks are skipped.
- If the same type appears more than once, the last ack wins.
- Parsed checkpoints are written with `CheckpointStore.set_many()`.
- After the write, the adapter updates `session.updated_at` and `sessions:by_updated_at`.

`SyncResetV1` is special: once the adapter sees a `SyncResetV1` ack, it ignores the rest of the batch, clears all checkpoints for the session, unsets `is_pending_sync_reset`, updates session activity, and returns.

### `DELETE /api/sync/ack`

- No `types` field: delete all checkpoints for the session.
- Non-empty `types`: delete only those entity types.
- Empty `types` list: no-op.

## Security and lifecycle notes

- **JWTs are encrypted at rest** in `session:{uuid}.stored_jwt`; clients never receive the backend JWT after login.
- **Session tokens are stable** across backend JWT refreshes, which keeps `/api/sessions` identities and sync checkpoints stable too.
- **Session deletion is authoritative**: deleting a session removes its checkpoint hash and index entries.
- **TTL is optional**: when a session is created with an expiry, the same TTL is applied to both `session:{uuid}` and `session:{uuid}:checkpoints` so they expire together.
- **Index cleanup is lazy**: if Redis TTL removes the main keys first, later reads clean up orphaned entries from `user:{user_id}:sessions` and `sessions:by_updated_at`.

## Related docs

- [`docs/references/session-checkpoint-reference.md`](../references/session-checkpoint-reference.md) — field-level Redis schema reference
- [`docs/architecture/sync-stream-architecture.md`](./sync-stream-architecture.md) — two-phase event streaming and foreign-key ordering
- [`docs/design-docs/auth-design.md`](../design-docs/auth-design.md) — broader auth/session design rationale
