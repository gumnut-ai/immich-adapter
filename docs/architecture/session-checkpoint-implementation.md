---
title: "Session and Checkpoint Implementation in immich-adapter"
last-updated: 2026-09-23
---

# Session and Checkpoint Implementation in immich-adapter

## Summary

`immich-adapter` keeps two related pieces of Redis state:

- **Sessions** keyed by a stable UUID session token that Immich clients send back on later requests
- **Checkpoints** keyed by session, sync epoch, and sync entity type so `/api/sync/stream` can resume from the last acknowledged cursor

The important distinction is that the client-facing token is **not** the backend JWT. The adapter encrypts the backend JWT, keeps it server-side, and updates it in place when the backend refreshes it. Sync resume is likewise **cursor-based**, not timestamp-hash-based: each entity type stores the last opaque Gumnut API events cursor that the client acknowledged.

## Redis data model

### Session keys

```text
session:{uuid}
  ├── user_id: "550e8400-e29b-41d4-a716-446655440000"
  ├── library_id: "lib_..."
  ├── stored_jwt: "<encrypted backend JWT>"
  ├── device_type: "iOS"
  ├── device_os: "iOS 18.5"
  ├── app_version: "1.136.0"
  ├── created_at: "2026-06-11T08:59:12.123456+00:00"
  ├── updated_at: "2026-06-11T09:04:55.654321+00:00"
  ├── sync_epoch: "0"
  ├── client_epoch: "0"
  ├── is_pending_sync_reset: "0"
  ├── library_checked_at: "1781168695.65"
  └── library_from_choice: "0"

user:{user_id}:sessions
  └── {session_uuid_1, session_uuid_2, ...}

sessions:by_updated_at
  └── {session_uuid -> updated_at_timestamp}
```

### Checkpoint keys

```text
session:{uuid}:checkpoints          # sync epoch 0
session:{uuid}:checkpoints:{epoch}  # later epochs
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

`AuthMiddleware` supports two credential paths:

- API-key clients send a Gumnut API key in `x-api-key`. The adapter forwards
  that key as the Gumnut credential without looking up a Redis session or
  performing a session JWT refresh.
- Session-token clients (web and mobile) use the UUID session token described
  below. For each authenticated request it:

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
- `PUT /api/sessions/{id}` currently supports toggling `isPendingSyncReset`:
  setting it advances the sync epoch; clearing it marks the client current.
- `DELETE /api/sessions/{id}` deletes a specific session.
- `POST /api/sessions` and `POST /api/sessions/{id}/lock` are still 204 stubs.

`library_id` is the Gumnut library the session's calls are scoped to (`""`
until the first scoped request resolves it). `library_checked_at` (epoch
seconds) and `library_from_choice` record when it was last resolved and
whether it was the user's stored choice. See
[Adapter Architecture](adapter-architecture.md#library-scope).

Sync resumes from opaque events cursors within one library, so sync state
belongs to the session's `sync_epoch`, which advances whenever the session
leaves a library (in the same conditional write that changes `library_id`) or
a reset is requested. `client_epoch` is the epoch the client's local copy
belongs to; the client owes a `SyncResetV1` while it differs from
`sync_epoch` (`isPendingSyncReset`). A hash written before epochs reads as
epoch 0, owing a reset if it had `is_pending_sync_reset: "1"`; writers keep
that field in step so an adapter version that predates epochs (during a
rolling deploy or rollback) can still read the session and owe the reset.

## Checkpoint model

Checkpoints are stored per session, sync epoch, and `SyncEntityType`. They are
written only while the session is still at their epoch and take the session's
remaining TTL, so they expire with it. Advancing the epoch deletes the previous
epoch's checkpoints, and deleting a session deletes its current ones. Epoch 0
keeps the key used before epochs, so clients mid-sync at an upgrade keep their
progress.

The current implementation stores **opaque cursors**, not last-synced timestamps. That matters for two reasons:

1. `/api/sync/stream` resumes by passing the stored cursor back to the backend events API as `after_cursor`.
2. A checkpoint's `updated_at` field is bookkeeping only; it is not the resume position.

Every ack the adapter issues carries the sync epoch of the stream that issued
it, and `GET /api/sync/ack` rebuilds acks from stored checkpoints the same way:

```text
SyncEntityType|cursor|epoch
```

Immich clients echo acks back unchanged, so the epoch round-trips without
client changes. Acks issued before epochs have an empty last field and count as
the session's current epoch.

Checkpoints without a cursor are skipped when reconstructing ack responses.

## Sync stream flow

### `POST /api/sync/stream`

The sync stream is driven by the backend events feed, with one checkpoint per entity type.

1. If `request.reset=true`, the adapter deletes all checkpoints for the session's current sync epoch before streaming.
2. The stream binds the session's current `sync_epoch`. If the client owes a reset, or the session no longer records the library this request bound, the adapter returns a one-event stream containing `SyncResetV1|reset|{epoch}` and stops.
3. Before the response starts, the route resolves `users.me()` so auth failures still surface as normal HTTP errors instead of being swallowed inside a streaming generator.
4. `AuthUsersV1` and `UsersV1` are emitted directly from the current user record, using `current_user.updated_at` (or the user id as a fallback) as their cursor.
5. Event-backed types resume from `checkpoint.cursor` using the backend events API with:
   - `after_cursor` for per-type resume
   - `created_at_lt=sync_started_at` for a bounded point-in-time window
6. Upserts stream first in foreign-key dependency order. Delete events are buffered and emitted afterward in reverse dependency order.
7. `AssetEditsV1` is accepted as a no-op request type because the sync stream
   has no edit-history source; this does not disable the implemented HTTP edit
   routes. `AssetFacesV1` is skipped when `AssetFacesV2` is also requested so
   the same face events are not streamed twice.
8. On successful completion, the stream finishes with `SyncCompleteV1|complete|`. Failures during event fetch or entity hydration end the stream without a completion event, leaving the affected cursor unacknowledged for retry.

This two-phase ordering is the key behavior that keeps the mobile client's SQLite foreign keys consistent while still using a single events source.

## Sync ack flow

### `GET /api/sync/ack`

Returns the checkpoints of the session's current sync epoch as `SyncAckDto[]`, rebuilding each ack as `SyncEntityType|cursor|epoch`.

### `POST /api/sync/ack`

The adapter parses each ack string as `SyncEntityType|cursor|epoch`.

- Invalid entity types return HTTP 400.
- Malformed strings and empty-cursor acks are skipped.
- If the same type appears more than once, the last ack wins.
- Acks for an epoch other than the session's current one are dropped: they
  come from a stream of a library the session has left.
- Parsed checkpoints are written with `CheckpointStore.set_many()`, which
  rechecks the epoch atomically.
- After the write, the adapter updates `session.updated_at` and `sessions:by_updated_at`.

`SyncResetV1` is special: once the adapter sees a `SyncResetV1` ack, it ignores the rest of the batch and, if the session is still at the ack's epoch, sets `client_epoch` to it and clears that epoch's checkpoints in one script (the client wipes its local copy before acking). It then updates session activity and returns. A reset ack for an epoch the session has left changes nothing; the next stream resets again.

### `DELETE /api/sync/ack`

- No `types` field: delete all checkpoints of the session's current epoch.
- Non-empty `types`: delete only those entity types.
- Empty `types` list: no-op.

## Security and lifecycle notes

- **JWTs are encrypted at rest** in `session:{uuid}.stored_jwt`; clients never receive the backend JWT after login.
- **Session tokens are stable** across backend JWT refreshes, which keeps `/api/sessions` identities and sync checkpoints stable too.
- **Session field updates are conditional and atomic**: `SessionStore` uses one
  Redis script for updates such as JWT refresh and activity, and conditional
  scripts for library and sync-epoch changes. If deletion wins the race, an
  update writes neither a partial hash nor the activity index.
- **Session deletion is authoritative**: deleting a session removes its checkpoint hash and index entries.
- **TTL is optional**: when a session is created with an expiry, `session:{uuid}` gets that TTL and checkpoint writes copy its remaining TTL, so they expire together.
- **Index cleanup is lazy**: if Redis TTL removes the main keys first, later reads clean up orphaned entries from `user:{user_id}:sessions` and `sessions:by_updated_at`.

## Related docs

- [`docs/references/session-checkpoint-reference.md`](../references/session-checkpoint-reference.md) — field-level Redis schema reference
- [`docs/architecture/sync-stream-architecture.md`](./sync-stream-architecture.md) — two-phase event streaming and foreign-key ordering
- [`docs/design-docs/auth-design.md`](../design-docs/auth-design.md) (deprecated) — broader auth/session design rationale
