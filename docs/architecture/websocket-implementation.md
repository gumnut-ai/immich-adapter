---
title: "WebSocket Implementation Documentation for immich-adapter"
last-updated: 2026-07-15
---

# WebSocket Implementation Documentation for immich-adapter

## Overview

This document describes the current Socket.IO implementation in immich-adapter, with a focus on authentication, room membership, and the event helpers used by the API routers.

**Transport note:** the WebSocket transport that Socket.IO sits on top of is provided by uvicorn, configured via `--ws websockets-sansio` in the Dockerfile and `.vscode/launch.json`. The default `--ws auto` would route through the deprecated legacy `websockets` API and leak `"exception in shielded future"` errors on peer close. Application code in this file does not interact with that layer directly. See `../references/uvicorn-settings.md` § "ws (WebSocket protocol implementation)" for the full rationale.

**Related Documentation:**

- Immich WebSocket Events Reference - Detailed reference of Immich's event triggers, payloads, and client handling
- Uvicorn Server Settings (`../references/uvicorn-settings.md`) - WebSocket protocol implementation, keep-alive, concurrency, and backlog settings

---

## 1. Architecture

### 1.1 Current Implementation

immich-adapter has a Socket.IO server in `services/websockets.py`. The ASGI wrapper is mounted at `/api/socket.io` by `main.py`:

```python
import socketio

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    engineio_version=4,
)

socket_app = socketio.ASGIApp(socketio_server=sio, socketio_path="/")
```

The server currently:

- Authenticates connections with an Immich session token.
- Joins each authenticated socket to both a user room and a session room.
- Emits `on_server_version` to the connecting socket.
- Tracks sockets for disconnect cleanup and logs connect/disconnect events.

### 1.2 Room-Based Messaging

Socket.IO rooms allow sending messages to specific subsets of connected clients:

```text
User A (2 devices)
    |
    +- Phone (sid: abc, session: session-1)
    +- Tablet (sid: def, session: session-2)

User room "user-A": [abc, def]
Session room "session-1": [abc]
Session room "session-2": [def]
```

Emitting to a user room reaches all of that user's connected devices. Emitting to a session room targets one session, which is how `on_session_delete` tells a specific client to log out.

**Room naming convention**: Use the Gumnut user ID and the session UUID as room names:

```python
await sio.enter_room(sid, session.user_id)
await sio.enter_room(sid, str(session.id))
```

The user room is shared by all of a user's sessions; the session room is unique to one session.

### 1.3 Session Tracking

To support disconnect cleanup and debugging, the service maintains a mapping of socket IDs to both room identities:

```python
# Maps socket ID -> (user ID, session ID)
_sid_to_user: dict[str, tuple[str, str]] = {}
```

This is accessed from async coroutines. Since Python's GIL and asyncio's single-threaded event loop handle this, no explicit locking is needed for dict operations.

---

## 2. Authentication

### 2.1 How Immich Clients Send Authentication

| Client | Authentication Method | Header/Cookie Name | Value |
|---|---|---|---|
| **Web** | HTTP Cookie (automatic) | `immich_access_token` | Session token (UUID) |
| **Mobile** | Custom Header | `x-immich-user-token` | Session token (UUID) |
| **Mobile (alt)** | Bearer Token | `Authorization: Bearer <token>` | Session token (UUID) |

Both clients send a **session token** (UUID), not a JWT. This token is used to look up the encrypted JWT in Redis.

### 2.2 Socket.IO `connect` Handler Environment

When a Socket.IO client connects, the `connect` handler receives:

```python
@sio.event
async def connect(sid, environ):
    # environ contains WSGI/ASGI environment variables
```

The `environ` dict includes:

- `HTTP_COOKIE` - Raw cookie string (e.g., `"immich_access_token=abc-123; other=value"`)
- `HTTP_X_IMMICH_USER_TOKEN` - Mobile client's custom header (note: HTTP_ prefix, underscores)
- `HTTP_AUTHORIZATION` - Bearer token if provided

### 2.3 Authentication Steps in `connect` Handler

```text
1. Extract session token from environ:
   a. Check HTTP_X_IMMICH_USER_TOKEN header (mobile)
   b. Check HTTP_AUTHORIZATION for Bearer token (mobile alt)
   c. Parse HTTP_COOKIE for immich_access_token (web)

2. If no token found:
   -> Return False to reject connection

3. Look up session in Redis:
   session = await session_store.get_by_id(session_token)

4. If session not found:
   -> Return False to reject connection

5. Extract the room identities from the session:
   user_id = session.user_id
   session_id = str(session.id)

6. Join the socket to both rooms:
   await sio.enter_room(sid, user_id)
   await sio.enter_room(sid, session_id)

7. Store mapping for cleanup:
   - sid -> (user_id, session_id)

8. Emit on_server_version to the client (existing behavior)

9. Return normally (the handler's implicit None accepts the connection)
```

---

## 3. Event System Design

### 3.1 Supported Events (Current Scope)

Starting with the current upload-success events, but designed for future extension:

| Event Name | Payload | Sent To | Trigger |
|---|---|---|---|
| `on_upload_success` | `AssetResponseDto` | Asset owner | Images: immediately after upload completes; videos: after the shared 3s WebSocket deferral |
| `AssetUploadReadyV1` | `{ asset: SyncAssetV1, exif: SyncAssetExifV1 }` | Asset owner | Emitted alongside `on_upload_success` on the same schedule |
| `on_asset_update` | `AssetResponseDto` | Asset owner | After `PUT /api/assets/{id}` updates metadata |
| `on_server_version` | Version info | Connecting client | On connect (existing) |

### 3.2 Event Implementation Status

### Phase 1: Can Implement Now (No Backend Changes)

| Event | Payload | Web | Mobile | Notes |
|---|---|---|---|---|
| `on_upload_success` | `AssetResponseDto` | Yes | Legacy | Images: emitted synchronously (CDN resizes the original — variants ready at upload time). Videos: emission deferred by `_VIDEO_EMIT_DELAY_SECONDS` (3s) in `routers/api/assets.py` so the still-image `derived_path` has time to materialize before the web client tries to render `/api/assets/{id}/thumbnail` — otherwise the timeline card shows "Error loading image" until refresh. |
| `AssetUploadReadyV1` | `SyncAssetV1` + `SyncAssetExifV1` | No | v2 sync | Emit alongside `on_upload_success` (shares the video deferral above) |
| `on_asset_delete` | `string` (assetId) | Yes | Yes | One per id; force=true permanent delete |
| `on_asset_trash` | `string[]` (assetIds) | Yes | Yes | Batched per chunk; force=false soft delete |
| `on_asset_restore` | `string[]` (assetIds) | Yes | Yes | Batched per chunk; restore from trash |
| `on_asset_update` | `AssetResponseDto` | Yes | Yes | Emitted from `PUT /api/assets/{id}` after a successful metadata edit |
| `on_session_delete` | `string` (sessionId) | Yes | No | Sessions managed by immich-adapter |
| `on_server_version` | `ServerVersionResponseDto` | Yes | No | Sent on connect (existing) |

### Phase 2: Requires the Gumnut API Event Channel

| Event | Payload | Web | Mobile | Notes |
|---|---|---|---|---|
| `on_person_thumbnail` | `string` (personId) | Page-specific | No | Cache busting after face detection |

### Not Applicable (Feature Not Supported)

These events require features that don't exist in the Gumnut API:

| Event | Reason |
|---|---|
| `on_asset_stack_update` | Stacks not implemented |
| `on_asset_hidden` | Asset visibility not supported |
| `on_notification` | Album sharing & job failures not implemented |
| `on_config_update` | Config management not implemented |
| `on_new_release` | Version checking service not implemented |

### Implementation Priority

1. **`on_upload_success` + `AssetUploadReadyV1`** - Core upload feedback for web and mobile
2. **`on_asset_delete` + `on_asset_trash` + `on_asset_restore`** - Timeline synchronization for hard-delete, soft-delete, and restore flows
3. **`on_session_delete`** - Security critical; forces web logout when session deleted

### 3.3 Mobile v2 Sync Protocol

Mobile clients using the v2 sync protocol listen to `AssetUploadReadyV1` instead of `on_upload_success`:

| Event | Payload | Notes |
|---|---|---|
| `AssetUploadReadyV1` | `{ asset: SyncAssetV1, exif: SyncAssetExifV1 }` | Compact format for real-time sync |

The mobile client batches these events and updates its local SQLite database immediately. Immich plans to deprecate `on_upload_success` in favor of `AssetUploadReadyV1`.

The implementation emits both upload-success events from `_do_emit_upload_events`, so images stay immediate and videos share the same 3-second deferral across web and mobile clients.

### 3.4 Event Emission Interface

The service keeps the room target explicit and exposes wrappers for the two supported scopes:

- `_emit_event` is the internal serializer and Socket.IO call. Callers should use a public wrapper instead.
- `emit_user_event` sends to every connected session for a user.
- `emit_user_event_per_id` gathers one user event per identifier for wire formats such as permanent-delete events.
- `emit_session_event` sends to one session room, currently used for `on_session_delete`.

The `WebSocketEvent` enum supplies type-safe event names, and `EventPayload` accepts a Pydantic model, string, string list, or `None`. `AssetUploadReadyV1Payload` is the Pydantic model used for the mobile upload event.

Usage:

```python
from services.websockets import (
    WebSocketEvent,
    emit_session_event,
    emit_user_event,
    emit_user_event_per_id,
)

# Pydantic models are serialized with model_dump(mode="json").
await emit_user_event(WebSocketEvent.ASSET_UPDATE, user_id, asset_dto)

# The one-id-per-event wire shape is gathered concurrently.
await emit_user_event_per_id(WebSocketEvent.ASSET_DELETE, user_id, asset_ids)

# Session-scoped events target the session UUID room.
await emit_session_event(WebSocketEvent.SESSION_DELETE, session_id, session_id)
```

---

## 4. Implementation Structure

### 4.1 Service module: `services/websockets.py`

The service owns the Socket.IO server, authentication callbacks, room tracking, event enum, and emission helpers:

1. `sio` is the Socket.IO server and `socket_app` is its ASGI wrapper. `main.py` mounts the wrapper at `/api/socket.io`.
2. `connect` extracts a session token from the mobile header, Bearer header, or web cookie, then looks up the session in the session store. Missing, unknown, or lookup-error sessions are rejected.
3. A successful connection joins both `session.user_id` and `str(session.id)` rooms, records the `(user_id, session_id)` tuple, and emits the current server version to that socket.
4. `disconnect` removes the socket from `_sid_to_user`; `connect_error` logs connection errors.
5. `_emit_event` serializes Pydantic payloads and performs the Socket.IO emit. The public wrappers catch and log `SocketIOError` so event transport failures do not fail the paired HTTP request.

The application mount is explicit:

```python
from services import websockets

app.mount("/api/socket.io", websockets.socket_app)
```

### 4.2 Usage from Assets Router

```python
# In routers/api/assets.py (after upload completes)

from services.websockets import emit_user_event, WebSocketEvent

async def _do_emit_upload_events(gumnut_asset, current_user):
    asset_response = convert_gumnut_asset_to_immich(gumnut_asset, current_user)
    await emit_user_event(WebSocketEvent.UPLOAD_SUCCESS, current_user.id, asset_response)

    payload = build_asset_upload_ready_payload(gumnut_asset, current_user.id)
    await emit_user_event(WebSocketEvent.ASSET_UPLOAD_READY_V1, current_user.id, payload)

async def _emit_upload_events(gumnut_asset, current_user):
    if gumnut_asset.mime_type.startswith("video/"):
        task = asyncio.create_task(_delayed_emit_upload_events(...))
        _pending_emit_tasks.add(task)
        task.add_done_callback(_pending_emit_tasks.discard)
        return

    await _do_emit_upload_events(gumnut_asset, current_user)
```

```python
# In routers/api/assets.py (after a permanent delete completes)

await emit_user_event_per_id(
    WebSocketEvent.ASSET_DELETE,
    user_id,
    (str(asset_id) for asset_id in chunk),
)
```

---

## 5. Key Considerations

### 5.1 Error Handling

- If `emit_user_event` is called for a user with no connected clients, it's a no-op (no error)
- Session lookup failures during connect result in connection rejection
- **Emit failures are swallowed centrally**: `emit_user_event` and `emit_session_event` catch `SocketIOError` from the underlying transport, log at WARN with `exc_info=True`, and return normally. Callers must NOT wrap these in `try/except SocketIOError` — the fire-and-forget contract is type-level so emit failures cannot break the request paired with them. If a caller needs to handle other exception types (e.g., DTO conversion before the emit), it can still wrap the broader block in its own try/except.

### 5.2 Payload Serialization

Use Pydantic's `model_dump(mode="json")` to ensure:

- Datetime objects are serialized as ISO strings
- UUIDs are serialized as strings
- Enums are serialized as their values

### 5.3 Mobile Client Behavior

The mobile client handles websocket events differently than web:

- **Pending changes queue**: Mobile batches incoming events with a 500ms debounce before processing
- **Offline support**: Events are queued and applied when the app becomes active
- **Batch uploads**: For sync operations, mobile uses a 5-10 second max wait to batch multiple uploads

This means the adapter doesn't need special batching logic -- mobile handles it client-side. However, be aware that rapid event emission (e.g., bulk operations) will be naturally debounced by the client.

---

## 6. Testing Strategy

`tests/unit/api/test_websockets.py` covers:

1. **Token extraction**: Mobile header, Bearer header, cookie, precedence, and malformed or missing values.
2. **Connection lifecycle**: Rejection for missing or invalid sessions, acceptance for valid sessions, joining both rooms, and `on_server_version` emission.
3. **Disconnect cleanup**: Removing known sockets and safely handling unknown sockets.
4. **Event helpers**: User- and session-scoped room targeting, Pydantic serialization, event names, and swallowed `SocketIOError` failures.

---

## 7. Summary

The implementation:

1. **Builds on** existing Socket.IO infrastructure in `services/websockets.py`, mounted at `/api/socket.io`
2. **Authenticates** websocket connections using the same session tokens as HTTP requests
3. **Joins** authenticated clients to both user and session rooms
4. **Exposes** user- and session-scoped event helpers with:
   - `WebSocketEvent` enum for type-safe event names
   - `EventPayload` type supporting Pydantic models, strings, string lists, or None
   - Automatic serialization of Pydantic models via `model_dump(mode="json")`
   - `emit_user_event_per_id` for one-id-per-event wire contracts
5. **Is extensible** - adding a supported event requires a new enum value and its router call site
