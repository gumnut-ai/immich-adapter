---
title: "Session and Checkpoint Implementation in immich-adapter"
last-updated: 2025-12-05
---

# Session and Checkpoint Implementation in immich-adapter

## Summary

To enable efficient incremental sync for mobile clients, immich-adapter must implement session tracking and checkpoint storage. This implementation is split between the adapter and Gumnut backend to balance clean separation of concerns with practical query performance.

**Key Design Decision - Split Implementation:**

**In immich-adapter Redis:**

- **Sessions** - Track authenticated device connections with tokens and metadata
- **Checkpoints** - Store sync progress per session per entity type

**In Gumnut metadata system:**

- **Update At** - Use existing `updated_at` timestamp on each object (asset, album, etc.)

This approach keeps protocol-specific session/checkpoint logic in the adapter while leveraging Gumnut's existing timestamp fields for sync ordering. The adapter uses the `updated_at` timestamp that already exists on all Gumnut objects.

---

## The Coordination Challenge

The core challenge is that checkpoints reference objects using timestamps, and Gumnut already provides these:

```text
+---------------------+         +------------------------------+
|   immich-adapter    |         |      Gumnut Backend          |
|                     |         |                              |
|  Checkpoint:        |         |  Asset:                      |
|  "AssetV1|2025-..." |<--------|  id: 12345                   |
|                     |         |  checksum: ...               |
|  Session:           |         |  library_id: ...             |
|  id, token, device  |         |  updated_at: 2025-01-20T...  |
|                     |         |                              |
+---------------------+         +------------------------------+
```

**Solution:** The adapter uses the existing `updated_at` timestamp from every Gumnut object. Sync filtering queries Gumnut for objects where `updated_at` is in the checkpoint range.

---

## Message Flow Between Server and Client during Sync

- Client requests which entity types it wants with POST /sync/stream
- Server responds with a stream of entities
- Client sends POST /sync/ack multiple times throughout the stream, not once at the end
- Client sends the LAST (highest timestamp) ack from each batch it processes
- Each Client POST to /sync/ack contains a single ack string (the latest from the batch)

```text
Server: Stream starts for AssetV1
  -> AssetV1|09:00:01|
  -> AssetV1|09:00:02|
  -> ... (100 items buffered by network)
  -> AssetV1|09:05:00|

Client: Receives batch, processes them locally
Client: POST /sync/ack { "acks": ["AssetV1|09:05:00|"] }  <- batch.last.ack

Server: Stores checkpoint: AssetV1|09:05:00|
Server: Continues streaming more AssetV1 items
  -> AssetV1|09:05:01|
  -> ... (another 100 items)
  -> AssetV1|09:10:00|

Client: Receives next batch, processes them
Client: POST /sync/ack { "acks": ["AssetV1|09:10:00|"] }  <- batch.last.ack

Server: Updates checkpoint: AssetV1|09:10:00|
Server: Entity type changes, streams AlbumV1
  -> AlbumV1|10:00:00|
  -> AlbumV1|10:00:01|
  -> ... (100 items)
  -> AlbumV1|10:05:00|

Client: New entity type detected, processes album batch
Client: POST /sync/ack { "acks": ["AlbumV1|10:05:00|"] }

Server: Stores checkpoint: AlbumV1|10:05:00|

... continues until SyncCompleteV1 message
```

### Redis Data Model

See linked document for description of the Session and Checkpoint Objects.

### Gumnut Backend - No Changes Required

Gumnut already provides `updated_at` timestamps on all entities.

---

## How Sync Filtering Works

### Without Checkpoints (Current State)

```text
1. Client: GET /sync/stream
2. Adapter: Query ALL assets from Gumnut
3. Adapter: Send all assets to client
4. Client: Process and acknowledge
5. [Next sync] Repeat steps 1-4 (full re-sync every time!)
```

### With Checkpoints (Target State)

```text
1. Client: GET /sync/stream with SyncStreamDto { types: ["AssetsV1", "AlbumsV1", "PeopleV1", ...] }
2. Adapter: Capture snapshot_time = NOW() (consistent point-in-time for ALL entity queries)
3. Adapter: Load ALL checkpoints for this session from Redis
   Example checkpoints:
   - "AssetV1|2025-01-20T10:00:00Z|"
   - "AlbumV1|2025-01-20T09:30:00Z|"
   - "PeopleV1|2025-01-19T14:00:00Z|"

4. FOR EACH entity type in client's request (in sync order):

   a. Get checkpoint for this entity type (or NULL if first sync)
   b. Query Gumnut with pagination:
      WHERE updated_at > checkpoint_time AND updated_at < snapshot_time
      ORDER BY updated_at, id
      LIMIT page_size
      - Lower bound: Last checkpoint for THIS entity type (what client already has)
      - Upper bound: Snapshot time (same for all entity types - prevents race conditions)
      - Pagination: Fetch in batches (e.g., 1000 items) to avoid memory issues
      - Stable ordering: ORDER BY updated_at, id ensures consistent results across pages
   c. FOR EACH page of results:
      - Stream entities to client with ack strings: "TYPE|<item.updated_at.isoformat()>|"
      - Client processes batch locally as it arrives
      - Client sends POST /sync/ack with [batch.last.ack] (INCREMENTAL - happens DURING stream)
      - Adapter stores checkpoint immediately (enables progressive crash recovery)
      - Continue until no more results

5. Adapter: Send SyncCompleteV1 message with snapshot_time
6. Stream ends
```

**Result:** Incremental sync with progressive checkpoint updates throughout the stream

**Key Points:**

- **One snapshot, multiple checkpoints**: snapshot_time captured once, but each entity type has independent checkpoint
- **Independent progress**: Assets can be at 10:00, Albums at 09:30, People at 14:00 yesterday
- **Snapshot consistency**: All entity queries use same upper bound timestamp (prevents partial views)
- **Race condition prevention**: Updates happening DURING sync are excluded (picked up in next sync)
- **Progressive acknowledgment**: Client acks batches INCREMENTALLY as they're processed, not all at once at the end
- **Crash recovery**: If client crashes mid-sync, already-acked batches don't need re-downloading
- **batch.last.ack**: Each ack contains the highest timestamp from the processed batch

---

## Affected Endpoints

### OAuth Login Flow

**Endpoints:** `POST /oauth/authorize`, `POST /oauth/callback`, `GET /oauth/mobile-redirect`

**Changes:**

- OAuth callback authenticates with Gumnut -> receive JWT
- Create session record using JWT hash
- Extract device metadata from User-Agent header
- Return Gumnut JWT in cookie (web) or redirect URL (mobile)

### POST /auth/logout

**Changes:**

- Extract Gumnut JWT from cookie/header (via existing auth middleware)
- Hash JWT to look up session: `session_id = sha256(jwt_token)`
- Delete session record (cascades to checkpoints)
- Clear authentication cookies
- If OAuth logout URL configured, redirect appropriately

### Session Management Endpoints

### GET /sessions

**New Implementation:**

- Query all sessions for authenticated user
- Return list with device info and last activity

**Response:**

```json
{
  "sessions": [
    {
      "id": "9f8e7d6c5b4a3210fedcba9876543210abcdef1234567890abcdef1234567890",
      "deviceType": "iOS",
      "deviceOS": "iOS 17.4",
      "appVersion": "1.94.0",
      "current": true,
      "updatedAt": "2025-01-15T10:30:00Z"
    },
    {
      "id": "a1b2c3d4e5f6071829384756abcdef0123456789fedcba9876543210abcdef12",
      "deviceType": "Chrome",
      "deviceOS": "macOS",
      "appVersion": null,
      "current": false,
      "updatedAt": "2025-01-14T08:00:00Z"
    }
  ]
}
```

**Note:** Session IDs are SHA-256 hashes (64 hex characters) of the Gumnut JWTs.

### DELETE /sessions/:id

**New Implementation:**

- Delete specific session (logout that device)
- Cascade delete associated checkpoints

### Sync Endpoints

### POST /sync/stream

**Current:** Returns all assets (no filtering)

**New Implementation:**

1. Extract Gumnut JWT from request (via existing auth middleware)
2. Hash JWT to get session ID
3. Capture snapshot timestamp ONCE: `snapshot_time = datetime.now(timezone.utc)` (consistent upper bound for all queries)
4. Load ALL checkpoints for this session from Redis
5. For each entity type in `SyncStreamDto.types` (in sync order):
   - Get checkpoint for THIS entity type (or use epoch if first sync)
   - Query Gumnut WITH PAGINATION: `WHERE updated_at > checkpoint_time AND updated_at < snapshot_time ORDER BY updated_at, id LIMIT page_size`
   - For each page of results:
     - Stream entities to client with ack strings: `"TYPE|<item.updated_at.isoformat()>|"`
     - Client processes batch and sends `POST /sync/ack` with `[batch.last.ack]` (INCREMENTAL - happens during stream)
     - Continue until no more results
6. Send `SyncCompleteV1` message with snapshot_time
7. Stream ends

**Important:** Client sends acks INCREMENTALLY throughout the stream as batches are processed, not all at once at the end. This enables crash recovery.

**Ack/Checkpoint String**

The ack/checkpoint string is generated by immich-adapter, and is only used by the mobile client as a token. The format is as follows:

`SyncEntityType|timestamp|` -- note trailing `"|"` - allows for future additions to the string format

`SyncEntityType` is the entity being synced, from a defined list of [47 values defined by Immich](https://github.com/gumnut-ai/immich-adapter/blob/70eff96f3db949d3568fdb1664953d69c8d60b16/routers/immich_models.py#L1460-L1507). Examples: `AssetV1`, `AssetDeleteV1`, `AlbumV1`

`timestamp` is a ISO 8601 date time format including millisecond resolution (the included in Python's `datetime.isoformat()`)

Example: `2025-01-20T10:30:45.123456+00:00`

**Note**: The immich implementation includes an optional third component to the string, "EXTRA_ID". We do not need to use it at this point.

**Example Flow:**

```python
# SERVER-SIDE CODE (immich-adapter streaming to client)
# Note: Client will call POST /sync/ack incrementally as it processes batches

import redis
import hashlib
from datetime import datetime, timezone

# Redis client (connection pooled, singleton)
redis_client = redis.Redis(host=REDIS_SERVER, port=REDIS_PORT, decode_responses=True)

# Get JWT from auth middleware
jwt_token = request.state.jwt_token

# Hash JWT to get session ID
session_id = hashlib.sha256(jwt_token.encode()).hexdigest()

# Capture snapshot time ONCE (used for all entity types)
snapshot_time = datetime.now(timezone.utc)

# Load session from Redis
session_data = redis_client.hgetall(f"session:{session_id}")
if not session_data:
    raise HTTPException(status_code=401, detail="Session not found")
library_id = session_data["library_id"]

# Load ALL checkpoints for this session from Redis (single HGETALL)
checkpoints_raw = redis_client.hgetall(f"session:{session_id}:checkpoints")
# Parse checkpoint values: "last_synced_at|updated_at"
checkpoint_map = {}
for entity_type, value in checkpoints_raw.items():
    last_synced_at_str, _ = value.split("|")
    checkpoint_map[entity_type] = datetime.fromisoformat(last_synced_at_str)

# Loop over each entity type requested by client
for entity_type in sync_stream_dto.types:
    last_synced_at = checkpoint_map.get(entity_type, datetime.min.replace(tzinfo=timezone.utc))

    # Query Gumnut with pagination
    page = 1
    page_size = 1000
    while True:
        assets = gumnut_client.assets.list(
            library_id=library_id,
            filters={  # TODO new functionality for Gumnut API
                "updated_at__gt": last_synced_at.isoformat(),
                "updated_at__lt": snapshot_time.isoformat()
            },
            order_by=["updated_at", "id"],  # TODO new functionality for Gumnut API
            limit=page_size,
            offset=(page - 1) * page_size
        )

        if not assets:
            break

        # Stream entities with ack (each item gets its own timestamp)
        for asset in assets:
            send_event({
                "type": "AssetV1",
                "data": map_asset(asset),
                "ack": f"AssetV1|{asset.updated_at.isoformat()}|"
            })

        # Note: After client processes this batch, it will call:
        # POST /sync/ack {"acks": ["AssetV1|<last_asset.updated_at>|"]}

        page += 1

# Send completion message
send_event({
    "type": "SyncCompleteV1",
    "ids": [snapshot_time.isoformat()],
    "data": {}
})
```

### POST /sync/ack

**Current:** No-op (discards acks)

**New Implementation:**

**Important:** This endpoint is called INCREMENTALLY throughout the stream, not once at the end. Client sends ack after processing each batch of entities.

```python
# Request: { "acks": ["AssetV1|2025-01-20T10:30:45.123456+00:00|"] }
# Note: Typically contains ONE ack (batch.last.ack), though spec allows multiple

import redis
import hashlib
from datetime import datetime, timezone

redis_client = redis.Redis(host=REDIS_SERVER, port=REDIS_PORT, decode_responses=True)

# Get JWT from auth middleware and hash to get session ID
jwt_token = request.state.jwt_token
session_id = hashlib.sha256(jwt_token.encode()).hexdigest()

now = datetime.now(timezone.utc)
now_timestamp = now.timestamp()

for ack_string in request.acks:
    parts = ack_string.split("|")
    entity_type = parts[0]
    timestamp_str = parts[1] if len(parts) > 1 else None

    if timestamp_str:
        # Store checkpoint value: "last_synced_at|updated_at"
        checkpoint_value = f"{timestamp_str}|{now.isoformat()}"

        # Upsert checkpoint in Redis (HSET creates or updates)
        redis_client.hset(
            f"session:{session_id}:checkpoints",
            entity_type,
            checkpoint_value
        )

# Update session activity timestamp
redis_client.hset(f"session:{session_id}", "updated_at", now.isoformat())

# Update activity index for cleanup queries
redis_client.zadd("sessions:by_updated_at", {session_id: now_timestamp})
```

**Call Pattern:**

```text
During sync stream:
  POST /sync/ack {"acks": ["AssetV1|2025-01-20T10:05:00|"]}
  -> Checkpoint updated to 10:05:00

  POST /sync/ack {"acks": ["AssetV1|2025-01-20T10:10:00|"]}
  -> Checkpoint updated to 10:10:00

  POST /sync/ack {"acks": ["AssetV1|2025-01-20T10:15:00|"]}
  -> Checkpoint updated to 10:15:00

  POST /sync/ack {"acks": ["AlbumV1|2025-01-20T11:00:00|"]}
  -> New entity type, new checkpoint created
```

---

## Performance Considerations

### Redis Performance Characteristics

**Session Operations (O(1) or O(n) where n = fields):**

- `HGETALL session:{id}` - O(n) where n = number of fields (~10 fields) = effectively O(1)
- `HSET session:{id} field value` - O(1)
- `EXISTS session:{id}` - O(1)

**Checkpoint Operations:**

- `HGETALL session:{id}:checkpoints` - O(n) where n = entity types (~47 max) = effectively O(1)
- `HSET session:{id}:checkpoints type value` - O(1)

**User Session Lookups:**

- `SMEMBERS user:{id}:sessions` - O(n) where n = sessions per user (typically <10)

**Cleanup Queries:**

- `ZRANGEBYSCORE sessions:by_updated_at 0 cutoff` - O(log n + k) where k = stale sessions

**Memory Footprint (estimated per session):**

- Session hash: ~500 bytes
- Checkpoints hash (all 47 types): ~2KB
- Index entries: ~100 bytes
- **Total per session: ~3KB**

**Gumnut Backend:**

- `updated_at` already indexed on all tables
- No additional indexes needed

### Redis Configuration Recommendations

```text
# Persistence (choose based on durability needs)
save 900 1          # RDB snapshot every 15 min if at least 1 key changed
appendonly yes      # AOF for better durability
appendfsync everysec

# Memory management
maxmemory 256mb                    # Adjust based on expected session count
maxmemory-policy volatile-lru     # Evict keys with TTL first
```

---

## Security Considerations

### JWT Storage (Defense in Depth)

- **Gumnut JWTs are hashed** (SHA-256) before using as session IDs
- Adapter never stores plaintext JWTs in Redis
- Client sends JWT in cookie/header; adapter hashes and looks up session
- **Why hash?** If Redis is compromised:
  - Attacker gets SHA-256 hashes, not working JWTs
  - Cannot reverse hashes to obtain valid tokens
  - Cannot impersonate users or access Gumnut backend
- **Defense in depth:** Even though JWTs have expiration, hashing prevents direct reuse from Redis backups/snapshots

### Session Lifecycle

- Session creation: Hash JWT -> store session hash + add to indexes
- Session lookup: Hash incoming JWT -> HGETALL by key
- Session deletion: Delete session hash, checkpoints hash, remove from indexes (atomic via pipeline)
- Natural expiration: When JWT expires, session effectively expires

### Session Expiration

- Inactive sessions (90+ days) auto-deleted by cleanup job using sorted set index
- Optional explicit expiration via Redis TTL on session key
- JWT expiration provides primary access control

### Checkpoint Integrity

- Checkpoints are session-scoped (users can't access other users' checkpoints)
- Library scoping prevents cross-tenant data leakage
- Timestamp-based queries respect library boundaries

---

## Gumnut Backend Requirements

For this implementation to work, Gumnut must provide:

### Timestamp Range Query API

The adapter needs a way to query objects by timestamp ranges. This is the core of incremental sync.

**Proposal:** Add `filters` and `order_by` parameters in `list()` methods:

```python
gumnut_client.assets.list(
    library_id="lib-001",
    filters={
        "updated_at__gt": "2025-01-20T10:00:00.000000Z",  # After last checkpoint
        "updated_at__lt": "2025-01-20T11:00:00.000000Z"   # Before current snapshot
    },
    order_by=["updated_at", "id"]  # Stable ordering critical!
)
```

**What this executes in Gumnut:**

```sql
SELECT *
FROM assets
WHERE library_id = 'lib-001'
  AND updated_at > '2025-01-20T10:00:00.000000Z'
  AND updated_at < '2025-01-20T11:00:00.000000Z'
ORDER BY updated_at ASC, id ASC;
```

**Filter syntax:**

- Standard Django-style filters for timestamp fields
- Operators: `__gt`, `__lt`, `__gte`, `__lte`, `__eq`
- Can combine with other filters

---

## Compatibility Notes

### Web Client

- Creates sessions (in Redis) but never calls sync endpoints
- Sessions used only for authentication
- No checkpoints stored for web sessions

### Mobile Client

- Creates sessions (in Redis) AND uses sync endpoints
- Checkpoints (in Redis) enable incremental sync
- Uses `updated_at` timestamps for filtering
- Each device maintains independent checkpoints

### API Keys

- Do NOT create sessions
- Cannot use sync endpoints (will return 403)
- Used for automation, scripts, CI/CD

---

## Success Metrics

### Performance

- **Initial sync:** Full asset download
- **Subsequent sync (no changes):** Minimal overhead (metadata only)
- **Subsequent sync (incremental):** Only changed assets sent
- **Sync latency:** Fast checkpoint lookup and query (Redis O(1) operation)

### Correctness

- No duplicate assets in mobile client
- Interrupted syncs resume correctly
- Multi-device sync maintains independent state
- Session deletion clears checkpoints

### Maintainability

- Adapter Redis schema independent of Gumnut
- Clear separation of concerns
- Can swap Gumnut for another backend without protocol changes
