---
title: "Session & Checkpoint Object Reference"
last-updated: 2025-12-05
---

# Session & Checkpoint Object Reference

This document describes the Redis data model for Session and Checkpoint objects in immich-adapter.

---

## Redis Data Model

### Overview

The adapter uses Redis (via redis-py 5.3.1) for session and checkpoint storage. This provides:

- Fast key-value lookups for session validation
- Atomic operations for checkpoint updates
- Built-in TTL support for session expiration
- Simple deployment (no separate database)

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

# Session activity index (Sorted Set) - enables stale session cleanup
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

**Session Identification:** The session ID is a UUID generated at login time. This UUID serves as both the session token (sent to clients as `accessToken`) and the Redis key. Unlike hashing the JWT, using a stable UUID ensures that:

- JWT refresh does not invalidate the session
- Checkpoints remain associated with the session across token refreshes
- Session revocation is immediate (delete session = revoke access)

### Session Expiration via TTL

Sessions can optionally expire using Redis TTL. When a session is created with an expiration time, set TTL on both the session and checkpoint keys:

```python
def create_session_with_expiration(redis: Redis, session_id: UUID, session_data: dict,
                                    user_id: str, expires_at: datetime | None = None):
    session_key = str(session_id)
    pipe = redis.pipeline()
    pipe.hset(f"session:{session_key}", mapping=session_data)
    pipe.sadd(f"user:{user_id}:sessions", session_key)
    pipe.zadd("sessions:by_updated_at", {session_key: time.time()})

    if expires_at:
        ttl_seconds = int((expires_at - datetime.now(timezone.utc)).total_seconds())
        if ttl_seconds > 0:
            pipe.expire(f"session:{session_key}", ttl_seconds)
            pipe.expire(f"session:{session_key}:checkpoints", ttl_seconds)

    pipe.execute()
```

To query remaining time until expiration:

```python
ttl_seconds = redis.ttl(f"session:{session_id}")
# Returns: positive int (seconds remaining), -1 (no TTL), -2 (key doesn't exist)
```

**Note:** When Redis expires a session key via TTL, the checkpoint key expires too (same TTL), but the index entries (`user:{user_id}:sessions` and `sessions:by_updated_at`) are not automatically cleaned. The stale session cleanup job handles orphaned index entries.

---

## User Sessions Index

**Key:** `user:{user_id}:sessions`
**Type:** Set

Contains all session UUIDs belonging to a user. Enables efficient lookup of all sessions for session management endpoints (e.g., `/api/sessions` to list all devices).

---

## Checkpoints

**Key:** `session:{uuid}:checkpoints`
**Type:** Hash

Each field is an entity type, and the value is a pipe-delimited string:

```text
{last_synced_at}|{updated_at}
```

**Example:**

```text
AssetV1: "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00"
```

**Why pipe-delimited instead of JSON?**

- Simpler parsing (no JSON library needed for basic ops)
- Smaller payload
- Both timestamps are fixed format, easy to split

**Why checkpoints are tied to sessions:**

- Each device (session) tracks its own sync progress independently
- When a session is deleted, its checkpoints are also deleted
- Client must re-sync from scratch if session is revoked
- JWT refresh does NOT affect checkpoints (session UUID is stable)

### Checkpoint Fields

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
- **Cleanup operations** - Delete checkpoints for sessions inactive > 90 days
- **Monitoring** - Alert if a session stops syncing

**Example use:**

```python
# Find stale sessions using sorted set
ninety_days_ago = time.time() - (90 * 24 * 60 * 60)
stale_sessions = redis.zrangebyscore("sessions:by_updated_at", 0, ninety_days_ago)

# Delete stale sessions and their data
for session_uuid in stale_sessions:
    delete_session(redis, session_uuid)
```

**Difference from `last_synced_at`:**

- `last_synced_at`: "Client processed data up to this time"
- `updated_at`: "We received this checkpoint at this time"

---

## Session Activity Index

**Key:** `sessions:by_updated_at`
**Type:** Sorted Set
**Score:** Unix timestamp of `updated_at`
**Member:** Session UUID (string)

Enables efficient queries for:

- Finding stale sessions (inactive > N days)
- Cleanup jobs

---

## Session Dataclass

```python
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class Session:
    """Session data stored in Redis."""

    id: UUID                      # The session token (what client sends as accessToken)
    user_id: str                  # Gumnut user ID (UUID format)
    library_id: str               # User's default library (or empty string)
    stored_jwt: str               # Encrypted Gumnut JWT
    device_type: str              # "iOS", "Android", "Chrome", etc.
    device_os: str                # "iOS", "macOS", "Android", etc.
    app_version: str              # "1.94.0" or empty for web
    created_at: datetime          # When session was created
    updated_at: datetime          # Last activity timestamp
    is_pending_sync_reset: bool   # True = client should full re-sync

    def to_dict(self) -> dict[str, str]:
        """Convert to Redis hash format (all values as strings)."""
        return {
            "user_id": self.user_id,
            "library_id": self.library_id,
            "stored_jwt": self.stored_jwt,
            "device_type": self.device_type,
            "device_os": self.device_os,
            "app_version": self.app_version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "is_pending_sync_reset": "1" if self.is_pending_sync_reset else "0",
        }

    @classmethod
    def from_dict(cls, session_id: UUID, data: dict[str, str]) -> "Session":
        """Create from Redis hash data."""
        return cls(
            id=session_id,
            user_id=data["user_id"],
            library_id=data["library_id"],
            stored_jwt=data["stored_jwt"],
            device_type=data["device_type"],
            device_os=data["device_os"],
            app_version=data["app_version"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            is_pending_sync_reset=data["is_pending_sync_reset"] == "1",
        )
```
