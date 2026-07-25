---
title: "Session & Checkpoint Object Reference"
last-updated: 2026-07-22
---

# Session & Checkpoint Object Reference

## Redis Data Model

### Overview

The adapter uses Redis for session and checkpoint storage, leaning on its built-in TTL for session expiration.

**Note:** This implementation uses only core Redis commands -- no RedisJSON, RediSearch, or other modules required.

### Session Token Architecture

The adapter generates a **separate session token** (a UUID) that is independent of the Gumnut JWT. This design:

- **Survives JWT refresh**: Gumnut may refresh the JWT, but the session token remains stable
- **Enables session revocation**: Deleting a session immediately revokes access
- **Supports checkpoints**: Sync checkpoints are tied to the stable session ID, not a changing JWT hash

**Authentication flow:**

1. User logs in via OAuth -> Gumnut returns JWT
2. Adapter generates a session token (UUID) and stores the encrypted JWT
3. Client receives the session token as `accessToken`
4. On each request, client sends session token -> adapter looks up session -> retrieves stored JWT for Gumnut API calls

### Key Schema

```text
# Session data (Hash) - with optional TTL for expiration
session:{uuid}
  ├── user_id: "user_123"
  ├── library_id: "lib_456"
  ├── stored_jwt: "<encrypted Gumnut JWT>"
  ├── device_type: "iOS"
  ├── device_os: "iOS"
  ├── app_version: "1.94.0"
  ├── created_at: "2025-01-20T10:00:00+00:00"
  ├── updated_at: "2025-01-20T10:30:00+00:00"
  └── is_pending_sync_reset: "0"

# User sessions index (Set) - enables "get all sessions for user"
user:{user_id}:sessions
  └── {uuid_1, uuid_2, ...}

# Checkpoints for a session (Hash) - all entity types in one key
session:{uuid}:checkpoints
  ├── AssetV1: "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00"
  ├── AlbumV1: "2025-01-20T09:30:00.000000+00:00|2025-01-20T09:30:00+00:00"
  └── PeopleV1: "2025-01-19T14:00:00.000000+00:00|2025-01-19T14:00:00+00:00"

# Session activity index (Sorted Set) - supports explicit stale-session maintenance
sessions:by_updated_at
  └── {uuid → updated_at_timestamp_score}
```

---

## Sessions

**Key:** `session:{uuid}`
**Type:** Hash
**TTL:** Optional - Redis automatically deletes expired sessions

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | string | Gumnut user ID (UUID format, converted from Gumnut's internal ID) |
| `library_id` | string | User's default library (empty string if not available) |
| `stored_jwt` | string | **Encrypted** Gumnut JWT - used for backend API calls |
| `device_type` | string | "iOS", "Android", "Chrome", etc. (from User-Agent parsing) |
| `device_os` | string | "iOS", "macOS", "Android", etc. (exact values for Immich UI icons) |
| `app_version` | string | "1.94.0" or empty for web (extracted from Immich mobile User-Agent) |
| `created_at` | string | ISO 8601 timestamp |
| `updated_at` | string | ISO 8601 timestamp |
| `is_pending_sync_reset` | string | "0" or "1" - When "1", server sends `SyncResetV1` message telling client to clear local data and full re-sync |

**Session Identification:** The session ID is a UUID generated at login time. This UUID serves as both the session token (sent to clients as `accessToken`) and the Redis key. Because it is independent of the JWT, it provides the stability and revocation properties described in [Session Token Architecture](#session-token-architecture).

### Session Expiration via TTL

Sessions can optionally expire using Redis TTL. When a session is created with an expiration time, the same TTL is applied to both the session key (`session:{uuid}`) and its checkpoint key (`session:{uuid}:checkpoints`) so they expire together.

**Note:** When Redis expires a session key via TTL, the checkpoint key expires too (same TTL), but the index entries (`user:{user_id}:sessions` and `sessions:by_updated_at`) are not automatically cleaned. The adapter does not run a background cleanup scheduler. `SessionStore.cleanup_stale_sessions()` is an explicit maintenance method, while normal reads through `get_by_user()` lazily remove orphaned entries when they encounter expired or corrupted sessions.

---

## User Sessions Index

**Key:** `user:{user_id}:sessions`
**Type:** Set

Contains all session UUIDs belonging to a user. Enables efficient lookup of all sessions for session management endpoints (e.g., `/api/sessions` to list all devices).

---

## Checkpoints

**Key:** `session:{uuid}:checkpoints`
**Type:** Hash

Each field is an entity type, and the value is a pipe-delimited `{last_synced_at}|{updated_at}` string (see the [Key Schema](#key-schema) for an example). Both timestamps are fixed ISO 8601 format, so the value is split on the single `|` rather than carrying a JSON wrapper.

**Why checkpoints are tied to sessions:**

- Each device (session) tracks its own sync progress independently
- When a session is deleted, its checkpoints are also deleted
- Client must re-sync from scratch if session is revoked
- Because the session UUID is stable across JWT refresh (see [Session Token Architecture](#session-token-architecture)), checkpoints survive token refreshes

### `last_synced_at` (First Component)

**What it is:** The timestamp extracted from the `ack` string (ISO 8601 format)

**Why needed:**

- **Sync filtering** - Used to query Gumnut for objects updated after this timestamp
- **Progress tracking** - Shows how far along sync has progressed for each entity type

**Example use:**

```python
# Parse from checkpoint value
checkpoint_value = redis.hget(f"session:{session_uuid}:checkpoints", "AssetV1")
last_synced_at, updated_at = checkpoint_value.split("|")
```

### `updated_at` (Second Component)

**What it is:** When this checkpoint was last modified (NOT the sync timestamp)

**Why needed:**

- **Session activity tracking** - Know when each session last acknowledged data
- **Cleanup operations** - Support explicit deletion of sessions that have been inactive past the cleanup threshold
- **Monitoring** - Alert if a session stops syncing

**How it's used:** `cleanup_stale_sessions` (in `services/session_store.py`) range-queries `sessions:by_updated_at` (a sorted set scored by `updated_at`) for sessions whose score is older than its `days` threshold, then deletes each stale session and its associated data. This is an explicit maintenance method; the adapter does not invoke it on a schedule.

---

## Session Activity Index

**Key:** `sessions:by_updated_at`
**Type:** Sorted Set
**Score:** Unix timestamp of `updated_at`
**Member:** Session UUID (string)

Enables efficient queries for:

- Finding stale sessions (inactive > N days)
- Supporting explicit stale-session maintenance

---

## Session Dataclass

The `Session` dataclass (in `services/session_store.py`) carries the fields documented in the [Sessions](#sessions) table, plus the `id` (the session UUID). Its `to_dict` / `from_dict` methods round-trip the dataclass to and from the Redis hash, serializing every value as a string (timestamps as ISO 8601, `is_pending_sync_reset` as `"0"`/`"1"`).
