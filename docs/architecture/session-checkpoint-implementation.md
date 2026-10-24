---
title: "Session and Checkpoint Implementation in immich-adapter"
last-updated: 2026-10-23
---

# Session and Checkpoint Implementation in immich-adapter

The adapter owns two related kinds of Redis state: sessions hold server-side
credentials behind a stable client token, and checkpoints hold the opaque
cursors the client has acknowledged. Checkpoints belong to a session's sync
epoch so progress from one library cannot become progress in another.

This page owns those boundaries and their lifecycle. The
[storage reference](../references/session-checkpoint-reference.md) owns Redis
records and serialization; the
[sync wire reference](../references/immich-sync-communication.md) owns request,
response, and acknowledgement contracts. `services/session_store.py`,
`services/checkpoint_store.py`, and `routers/api/sync/routes.py` own the
implementation mechanics.

## Credential custody and refresh

OAuth login exchanges the client's callback with the Gumnut API. The adapter
encrypts the returned JWT in Redis and returns a fresh UUID session token as
`accessToken`, also setting auth cookies. The client-facing token is independent
of the Gumnut JWT: refreshing that JWT changes server-side custody without
changing the client's identity or checkpoint namespace. A stale or replayed
callback rejected by the Gumnut API remains an HTTP 400 so the user can restart
login. `routers/api/oauth.py` owns the exchange and response.

`AuthMiddleware` in `routers/middleware/auth_middleware.py` accepts session
tokens through Bearer authorization, `x-immich-user-token`, or the
`immich_access_token` cookie. It resolves and decrypts the stored JWT for
Gumnut API calls. API-key clients instead send a Gumnut API key in `x-api-key`;
that credential passes through without a Redis session or JWT refresh.

The middleware persists a refreshed JWT captured before the downstream response
starts and strips its refresh header from the client response. Refresh state
captured while a streaming body is generated afterward is dropped. The session
keeps its current JWT, and the Gumnut API's sliding refresh reissues a token on a
later non-streaming request. Clients continue using the same UUID throughout.

Logout clears auth cookies and attempts to delete the current session; a store
failure is logged without preventing cookie clearing. Session management can
also remove a specific session or all other sessions for a user. The HTTP
behavior and DTOs live in `routers/api/auth.py` and `routers/api/sessions.py`.

## Library and epoch isolation

A session's `library_id` is the Gumnut library its calls are scoped to, initially
empty until a scoped request resolves it. The library-resolution metadata and
selection rules are described in
[Library scope](adapter-architecture.md#library-scope).

Sync cursors are library-specific, so leaving a resolved library advances
`sync_epoch` in the same conditional write that changes `library_id`. First
resolution from an empty library keeps the epoch. An explicit session reset
also advances it without changing the library. Both transitions delete the
previous epoch's checkpoints.

`client_epoch` identifies the client's local copy. While it differs from
`sync_epoch`, the session owes a reset (`isPendingSyncReset`). A stream binds
its session's current epoch and library. If a reset is owed, or the session no
longer records the library bound to that request, it sends only `SyncResetV1`.
This gate precedes the stream request's `reset` flag, so clearing checkpoints
cannot bypass a required client reset.

Acknowledgements carry the epoch of the stream that issued them. Checkpoint
writes check the epoch and session existence atomically, preventing a delayed
ack from restoring progress for a library the session has left. The client
wipes its local copy before acknowledging `SyncResetV1`; accepting that ack
atomically marks the client current and clears that epoch's checkpoints. A
reset ack for an epoch the session has left changes no reset or checkpoint
state, so a later stream requests a reset again. Reset acknowledgements take
precedence over other acknowledgements in their batch; parsing and legacy ack
compatibility belong to the [wire reference](../references/immich-sync-communication.md#acknowledgements).

Epoch 0 preserves the checkpoint namespace used before epochs. Sessions written
before epoch fields existed read as epoch 0 and still owe a reset if their
legacy pending-reset flag was set. Writers keep that flag in step for an
adapter version predating epochs during a rolling deploy or rollback. The
[storage reference](../references/session-checkpoint-reference.md#session-records)
owns the field and key contracts.

## Resume and recovery

A checkpoint records the last cursor the client acknowledged for one sync
entity type. Event-backed streams resume by passing that opaque cursor to the
Gumnut API events feed; a checkpoint's write timestamp is inspection metadata,
not the resume position or session activity time. User-derived rows instead
use the current user's update timestamp, with the user ID as a fallback.

The sync route resolves the current user before starting the response, allowing
auth failures to remain normal HTTP errors. Event reads share the first
response's `as_of` bound, while hydration and foreign-key checks read current
state and may reflect later writes. Parent-first upserts and child-first
deletes protect the client's local foreign keys. The
[sync stream architecture](sync-stream-architecture.md) owns ordering,
derived streams, and hydration failure rules.

A successful normal stream ends with `SyncCompleteV1`. Fetch or hydration
failure ends it without completion, leaving undelivered positions
unacknowledged for retry.
This does not guarantee replay of every buffered delete: an interrupted delete
phase can leave its older cursor behind an acknowledged upsert cursor and
retain stale client rows until a full sync. See
[Interrupted delete phase](sync-stream-architecture.md#interrupted-delete-phase)
for that recovery tradeoff. A stream request with `reset=true` clears the
current epoch's checkpoints for a full replay; it does not advance the epoch
or acknowledge a client reset.

Malformed stored checkpoint values are logged and skipped, so their entity
types resume from the normal starting position rather than an ambiguous
cursor. A missing session instead returns HTTP 401; lookup or JWT-decryption
exceptions return HTTP 500 without creating a replacement session. The
[storage reference](../references/session-checkpoint-reference.md#compatibility-and-corruption-behavior)
describes the accepted checkpoint serialization and corruption boundary.

## Security and lifecycle notes

- **JWT custody stays server-side.** Stored JWTs are encrypted; Immich clients
  receive the stable UUID session token.
- **Updates cannot resurrect deleted sessions.** Session field and activity
  updates are conditional and atomic. If deletion wins the race, an update
  writes neither a partial hash nor an activity-index entry. Library and
  sync-epoch transitions are conditional too.
- **Deletion removes owned state.** Normal session deletion removes its hash,
  current epoch's checkpoints, and user/activity index entries. Epoch changes
  discard the previous checkpoint namespace; expired sessions cannot accept
  new checkpoint writes.
- **TTL is optional.** When configured at session creation, checkpoint writes
  copy the session's remaining TTL so both expire together.
- **Index cleanup is lazy.** Redis expiry leaves user/activity index entries
  behind. User-session reads prune orphans; stale-session cleanup is an
  explicit store operation, not a background scheduler. The storage reference
  owns the [record and index details](../references/session-checkpoint-reference.md#session-records).

For the historical reason the adapter uses session tokens rather than raw
JWTs, read the deprecated [authentication design](../design-docs/auth-design.md).
