---
title: "Immich Adapter Architecture"
last-updated: 2026-03-19
---

# Immich Adapter Architecture

## Overview

The immich-adapter is a Python FastAPI backend that sits between Immich clients (web and mobile apps) and Gumnut's Photos API. Its job is protocol translation: it accepts native Immich API calls and converts them into Gumnut SDK calls, returning Immich-formatted responses.

```
Immich Client (web/mobile)
        │
        ▼
  immich-adapter (FastAPI, port 3001)
  ├── Auth middleware: session token → JWT lookup
  ├── Route handlers: translate request → Gumnut SDK call
  ├── WebSocket server: real-time events via Socket.IO
  └── Redis: sessions, checkpoints, encrypted JWTs
        │
        ▼
  Photos API (port 8000)
  ├── JWT validation (Clerk)
  ├── Business logic (SQLAlchemy, PostgreSQL + pgvector)
  └── Celery workers (ML, image processing)
```

Immich clients are unmodified — either the original open-source Immich apps or lightly customized forks. The adapter conforms to Immich's OpenAPI spec so clients work without changes. Gumnut may not support the latest Immich client version if it introduces breaking API changes.

### What the adapter does

- **Protocol translation** — Accepts Immich OpenAPI requests, converts to Gumnut SDK calls, returns Immich-formatted responses
- **Session management** — Generates session tokens at login, stores encrypted Gumnut JWTs in Redis
- **Incremental sync** — Manages per-session checkpoints for mobile sync, implements two-phase event ordering
- **WebSocket events** — Distributes real-time upload/delete notifications to connected devices
- **Static file serving** — Serves the Immich web UI

### What it doesn't do

- **JWT validation** — The backend validates JWT claims; the adapter just stores and forwards them
- **OAuth implementation** — OAuth flows are delegated to the backend
- **Authorization** — The backend enforces access control via JWT claims
- **User data storage** — All user data lives in Gumnut; the adapter only stores session metadata in Redis
- **Image processing** — ML inference, thumbnail generation, etc. are handled by Celery workers in the backend

## Authentication and Session Management

The adapter uses a **session token architecture** that decouples client authentication from backend JWT lifecycle. This is necessary because Immich clients expect stable authentication tokens, while Gumnut JWTs have short lifetimes and refresh frequently.

### Login flow

Both web and mobile clients authenticate via OAuth (Immich's `/api/auth/login` email/password endpoint exists as a stub but is not functional). The flow:

1. Client calls `POST /api/oauth/authorize` → adapter forwards to Photos API → returns Clerk OAuth URL
2. Client opens the OAuth URL in a browser → user authenticates with Clerk
3. Clerk redirects back to the adapter's `POST /api/oauth/callback` with an authorization code
4. Adapter exchanges the code with Photos API for a JWT, generates a UUID session token, encrypts the JWT, stores it in Redis
5. Client receives the session token (via `immich_access_token` cookie for web, `accessToken` in JSON body for mobile)

For mobile, OAuth providers that don't support custom URL schemes (e.g., `app.immich:///`) are handled via `GET /api/oauth/mobile-redirect`, which receives the OAuth response at an HTTPS URL and redirects to the mobile app's custom scheme.

### Request flow

1. Auth middleware extracts the session token from the request (cookie, `Authorization: Bearer`, or `x-immich-user-token` header)
2. Looks up the encrypted JWT in Redis using the session token
3. Decrypts the JWT and creates a Gumnut SDK client authenticated with it
4. Route handler uses the SDK client to make API calls
5. On response, middleware checks for `x-new-access-token` header (backend JWT refresh)
6. If present, updates the stored JWT in Redis — the client's session token remains unchanged

### Session storage (Redis)

```
session:{uuid}             → Hash { user_id, device_type, device_os, app_version, ... }
session:{uuid}:checkpoints → Hash { "asset_v1": "opaque_cursor|", "album_v1": "opaque_cursor|", ... }
user:{user_id}:sessions    → Set { session_uuid_1, session_uuid_2, ... }
sessions:by_updated_at     → Sorted Set { session_uuid: timestamp, ... }
```

Session storage is ~3KB per device, enabling horizontal scaling of the adapter.

### Session lifecycle

- **TTL**: Session Redis keys are set to expire based on the underlying JWT's expiry time. When a JWT is refreshed, the TTL is updated. Sessions with no expiry persist until stale cleanup (90+ days inactive).
- **Cookie flags**: `HttpOnly`, `Secure` (protocol-aware — disabled for local HTTP dev), `SameSite=lax`
- **Logout**: Deletes the Redis session key, clears cookies, emits `on_session_delete` WebSocket event to notify connected clients
- **JWT refresh failure**: If the backend cannot refresh an expired JWT, the next request using that session returns 401. The client must re-authenticate via OAuth.

**Related docs:**
- `docs/design-docs/auth-design.md` — Full auth architecture and OAuth flow design
- `docs/architecture/session-checkpoint-implementation.md` — Session and checkpoint storage details

## Data Translation Layer

### ID translation

Gumnut uses prefixed short UUIDs (e.g., `asset_BM3nUmJ6fkBqBADyz5FEiu`), while Immich uses standard UUIDs. The `routers/utils/gumnut_id_conversion.py` module handles bidirectional conversion using the `shortuuid` library.

| Entity | Gumnut prefix | Example |
|--------|---------------|---------|
| Asset | `asset_` | `asset_BM3nUmJ6fkBqBADyz5FEiu` |
| Album | `album_` | `album_K7xFp2mNqRsTvWyZ3aB4cD` |
| Person | `person_` | `person_J5wEn1lMpQrStUxY2zA3bC` |
| Face | `face_` | `face_H4vDm0kLoOnRtTwX1yA2bB` |
| User | `intuser_` | `intuser_G3uCl9jKnNmQsSvW0xZ1aA` |

All Gumnut IDs are encoded using the `shortuuid` library and are deterministically convertible to/from standard UUIDs (e.g., `asset_BM3nUmJ6fkBqBADyz5FEiu` ↔ `550e8400-e29b-41d4-a716-446655440000`). Immich clients always see standard UUIDs.

### Model mapping

Each entity type has a dedicated conversion module in `routers/utils/`:

| Module | Gumnut type | Immich type | Key mappings |
|--------|------------|-------------|--------------|
| `asset_conversion.py` | `AssetResponse` | `AssetResponseDto` | `local_datetime` → `fileCreatedAt`, `mime_type` → `type` (IMAGE/VIDEO/AUDIO/OTHER), EXIF extraction |
| `album_conversion.py` | `AlbumResponse` | `AlbumResponseDto` | `name` → `albumName`, `album_cover_asset_id` → `albumThumbnailAssetId` |
| `person_conversion.py` | `PersonResponse` | `PersonResponseDto` | `is_favorite` → `isFavorite`, `thumbnail_face_url` → `thumbnailPath`, null name → "Unknown Person" |

### Field naming convention

Gumnut uses `snake_case` (Python convention), Immich uses `camelCase` (TypeScript convention). The conversion functions handle this mapping explicitly rather than using automatic case conversion, since some fields have non-trivial transformations (e.g., `mime_type` → `type` enum, EXIF data extraction).

## Pagination and List Translation

Gumnut's Photos API uses **cursor-based pagination** (`limit` + `starting_after_id`), while Immich clients expect **offset-based pagination** (`page` + `size`). The adapter bridges this gap differently depending on the endpoint.

### Pattern 1: Load-all with client-side pagination

Used when Immich clients expect offset-based pagination or need the full result set for client-side features (e.g., total counts, filtering).

**How it works:**
1. Exhaust the Gumnut SDK's async paginator: `[p async for p in client.entity.list()]`
2. Apply any filters (e.g., `withHidden`)
3. Apply sorting (e.g., people endpoint sorts to match Immich's expected order)
4. Slice for the requested page: `all_items[(page-1)*size : page*size]`
5. Return with `total`, `hasNextPage`, and other metadata

**Endpoints using this pattern:**

| Endpoint | SDK call | Client-side logic |
|----------|----------|-------------------|
| `GET /api/people` | `client.people.list()` | Filter hidden → sort (hidden, favorite, named, asset count, alphabetical, created_at) → paginate |
| `GET /api/albums` | `client.albums.list()` | Convert all to list, no pagination exposed |
| `GET /api/assets/statistics` | `client.assets.list()` | Count total/images/videos from full set |
| `GET /api/people/{id}/statistics` | `client.assets.list(person_id=...)` | Count all assets for person |

**Performance implications:** Memory usage scales with total entity count, not page size. For a library with 10,000 people, every `GET /api/people` request loads all 10,000 into memory. This is acceptable for current Gumnut library sizes but will need optimization (e.g., server-side sorting support in Photos API) as libraries grow.

### Pattern 2: Server-side cursor pagination

Used when the adapter can leverage Photos API's cursor-based pagination internally, even though the external interface may differ. The Photos API has two cursor mechanisms depending on the endpoint:

- **Entity list endpoints** (assets, people, albums): `limit` + `starting_after_id` (cursor is an entity ID)
- **Events endpoint**: `limit` + `after_cursor` (cursor is an opaque position token)

Both support optional time-bound filters (e.g., `local_datetime_before`, `created_at_lt`) that constrain the result set but are not themselves cursors.

**How it works:**
1. Call Photos API with a `limit` and cursor parameter
2. Check `response.has_more` for next page
3. Advance the cursor (last entity ID or returned cursor token) for subsequent pages

**Endpoints using this pattern:**

| Endpoint | SDK call | Cursor + filters |
|----------|----------|-----------------|
| `GET /api/timeline/buckets` | `client.assets.counts(group_by="month")` | `starting_after_id` cursor, `local_datetime_before` filter, paginate until `has_more=false` |
| Sync stream (internal) | `client.events.get(...)` | `after_cursor` opaque cursor, `created_at_lt` time bound |

Note: Even with cursor pagination, the timeline buckets endpoint still loads all pages before returning to the client, since Immich expects the complete bucket list.

### Pattern 3: Date-range filtering

Used for timeline bucket contents where the date range is known in advance.

**How it works:**
1. Parse the `timeBucket` parameter to determine month boundaries
2. Query with `local_datetime_after` and `local_datetime_before` as half-open interval `[month_start, next_month_start)`
3. Load all assets within the range

**Endpoints using this pattern:**

| Endpoint | SDK call | Filter mechanism |
|----------|----------|-----------------|
| `GET /api/timeline/bucket` | `client.assets.list(extra_query={local_datetime_after, local_datetime_before})` | Date range from timeBucket param |

### Pattern 4: Single entity fetch

Used for detail endpoints where no pagination is needed.

**Endpoints:** `GET /api/assets/{id}`, `GET /api/people/{id}`, `GET /api/albums/{id}`, etc.

### Pagination constants

`PHOTOS_API_MAX_PAGE_SIZE = 200` — Used as the `limit` parameter when internally paginating Photos API responses (defined in `routers/api/constants.py`).

### Offset-based pagination limitations

Immich clients use offset-based pagination (`page`/`size`), which is inherently fragile when the underlying data changes between page requests. If an entity is added or removed between page 1 and page 2, clients may see duplicates or skip items. This is a fundamental limitation of the Immich pagination model, not something the adapter can fix — cursor-based pagination (which Gumnut uses internally) avoids this problem by anchoring to a specific item rather than an offset.

## Sorting and Ordering

The adapter must return entities in the order Immich clients expect, which may differ from Gumnut's default ordering.

### People ordering

Gumnut returns people ordered by `created_at DESC` (newest first). Immich clients expect:

1. **Hidden status** — visible people first (`is_hidden ASC`)
2. **Favorite status** — favorites first (`is_favorite DESC`)
3. **Named people first** — non-empty name before empty/null
4. **Asset count descending** — people appearing in more photos first
5. **Name alphabetically** — A-Z within same asset count tier
6. **Creation date ascending** — oldest first as tiebreaker

The adapter applies this sort in memory before pagination slicing (see `_immich_people_sort_key` in `routers/api/people.py`).

### Timeline ordering

Timeline bucket contents are returned in reverse chronological order by default (`local_datetime` descending). The `order` query parameter can reverse this to ascending.

### Album and asset ordering

Albums and assets use Gumnut's default ordering and are not re-sorted by the adapter.

## Sync Protocol

The adapter implements Immich's incremental sync protocol, allowing mobile clients to stay in sync with the backend without re-downloading everything on each app open.

### Two-phase streaming

The sync stream (`/api/sync/stream`) yields events in two phases to prevent FK constraint violations in the mobile client's SQLite database:

1. **Phase 1 — Upserts:** All new/updated entities in FK dependency order (parents before children):
   assets → albums → album_assets → exif → people → faces

2. **Phase 2 — Deletes:** All deletions in reverse FK order (children before parents):
   faces → album_assets → people → albums → assets

### Checkpoint system

Each session maintains per-entity-type checkpoints in Redis, stored as opaque cursor strings from the Photos API events endpoint. The ack string format is `"{entity_type}|{cursor}|"` (e.g., `"asset_v1|eyJ0eXAi...|"`). On the next sync:

1. Adapter captures `snapshot_time = NOW()` as a consistent upper bound
2. For each entity type, queries Photos API events with `after_cursor` (from the last checkpoint) and `created_at_lt` (the snapshot time)
3. Photos API handles ordering and tie-breaking — entities with the same timestamp are ordered by cursor position
4. Streams entities to the client with ack strings
5. Client sends `POST /sync/ack` incrementally during the stream (not at the end), enabling crash recovery — if the client crashes mid-sync, it resumes from the last acknowledged cursor
6. Adapter updates the checkpoint cursor in Redis

**Related docs:**
- `docs/architecture/session-checkpoint-implementation.md` — Checkpoint storage and coordination details
- `docs/design-docs/sync-stream-event-ordering.md` — FK ordering design rationale
- `docs/references/immich-sync-communication.md` — Immich sync protocol message formats

## WebSocket Events

The adapter runs a Socket.IO server for real-time notifications to connected Immich clients.

### Room-based messaging

Each authenticated user joins a room named with their Gumnut user ID. All of a user's devices (phone, tablet, browser) join the same room, so events are broadcast to all connected devices simultaneously.

### Events

| Event | Trigger | Payload |
|-------|---------|---------|
| `on_server_version` | Client connects | Server version info |
| `on_upload_success` | Asset uploaded via adapter | Asset ID and metadata |
| `on_asset_delete` | Asset deleted via adapter | Deleted asset IDs |
| `on_session_delete` | Session invalidated | Session ID |

**Related docs:**
- `docs/architecture/websocket-implementation.md` — Socket.IO setup, room management, event handling

## Error Handling

### Error response format

All HTTP errors conform to Immich's expected format:

```json
{
  "message": "Human-readable description",
  "statusCode": 401,
  "error": "Unauthorized"
}
```

Route handlers raise `HTTPException(status_code=..., detail="...")` and a global handler formats the response. Middleware returns `JSONResponse` directly (since `HTTPException` doesn't work in `BaseHTTPMiddleware`).

### Rate limit protection

Immich clients have no HTTP 429 handling — a rate limit response causes sync failures, broken thumbnails, and upload errors with no automatic recovery. The adapter protects against this:

1. The Gumnut SDK (Stainless-generated) has built-in retry with exponential backoff and jitter for 429/5xx responses
2. If SDK retries are exhausted, `map_gumnut_error` catches `RateLimitError` and returns **502 Bad Gateway** (not 429) to Immich clients. 502 is semantically correct — the adapter is a gateway and the upstream is unavailable. 503 would imply the adapter itself is overloaded, which isn't the case.
3. Immich clients display a generic error on 5xx and do not automatically retry — there is no risk of tight retry loops from the client side
4. Custom retry wrappers must not be added on top of SDK retry (causes retry amplification)

### Per-item error handling

Bulk operations (delete assets, update people, add assets to albums) process items individually and track per-item results. A failure on one item doesn't abort the entire operation — the adapter continues processing remaining items and returns a result array with success/error status per item.

## Endpoint Implementation Status

The adapter implements a subset of Immich's API surface. Unimplemented endpoints return stub responses (empty lists, 204 No Content, or hardcoded values) so Immich clients don't break.

### Fully implemented

| Area | Endpoints | Notes |
|------|-----------|-------|
| Assets | Upload, download (original + thumbnail), delete, bulk delete, existence check, statistics | Streaming downloads via `StreamingResponse` |
| Albums | CRUD, add/remove assets, statistics | User sharing not supported (returns 501) |
| People | CRUD, list with pagination/sort/filter, thumbnails, statistics | Merge is a stub |
| Faces | List, delete, reassign | Create is a stub |
| Timeline | Time buckets (monthly), bucket contents | Date-range filtering with timezone handling |
| Search | Smart search, metadata search, person search, statistics | Places, suggestions, explore are stubs |
| Sync | Full sync, delta sync, stream, ack | Two-phase ordering, checkpoint management |
| Auth | OAuth login/callback, logout, session management | Clerk OAuth via Photos API |
| WebSockets | Real-time upload/delete notifications | Socket.IO with room-based messaging |

### Stub implementations

| Area | Why stubbed |
|------|-------------|
| Faces | Create is a stub (SDK doesn't support face creation) |
| Libraries | Gumnut has a different library model |
| Tags | Not yet implemented in Gumnut |
| Map | Location data not yet surfaced |
| Memories | Auto-generated memories not supported |
| Asset metadata (custom) | Gumnut doesn't support arbitrary key-value metadata |
| Notifications | Push notifications not implemented |
| Partners | User sharing not implemented |
| Duplicates | Duplicate detection handled differently in Gumnut |

## Key Files

| File/Directory | Purpose |
|----------------|---------|
| `routers/api/` | All HTTP route handlers, organized by Immich API domain |
| `routers/api/sync/` | Sync protocol implementation (stream, ack, checkpoint) |
| `routers/api/constants.py` | Shared constants (`PHOTOS_API_MAX_PAGE_SIZE = 200`) |
| `routers/middleware/auth_middleware.py` | Session token extraction, JWT lookup, token refresh |
| `routers/utils/gumnut_id_conversion.py` | Bidirectional Gumnut ↔ Immich ID conversion |
| `routers/utils/asset_conversion.py` | Asset model translation and EXIF extraction |
| `routers/utils/album_conversion.py` | Album model translation |
| `routers/utils/person_conversion.py` | Person model translation |
| `routers/utils/error_mapping.py` | Gumnut SDK exceptions → HTTPException mapping |
| `routers/immich_models.py` | Pydantic models matching Immich's OpenAPI spec |
| `config/` | Settings, Redis, Sentry configuration |
