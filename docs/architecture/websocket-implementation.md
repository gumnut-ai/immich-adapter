---
title: "WebSocket Implementation Documentation for immich-adapter"
last-updated: 2026-01-09
---

# WebSocket Implementation Documentation for immich-adapter

## Overview

This document describes how to implement WebSocket support in immich-adapter, with a focus on authentication and the extensible event system needed to support `on_upload_success` and future events.

**Related Documentation:**

- Immich WebSocket Events Reference - Detailed reference of Immich's event triggers, payloads, and client handling

---

## 1. Architecture

### 1.1 Current Implementation

immich-adapter already has a Socket.IO server in `routers/api/websockets.py`:

```python
import socketio

# Socket.IO server with ASGI mode
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    engineio_version=4,
)

# ASGI app mounted at /socket.io
socket_app = socketio.ASGIApp(socketio_server=sio, socketio_path="/")
```

The server currently:

- Accepts all connections (no authentication)
- Emits `on_server_version` on connect
- Logs connect/disconnect events

### 1.2 Room-Based Messaging

Socket.IO rooms allow sending messages to specific subsets of connected clients:

```text
User A (2 devices)          User B (1 device)
    |                           |
    +- Phone (sid: abc)         +- Browser (sid: xyz)
    +- Tablet (sid: def)

Room "user-A": [abc, def]
Room "user-B": [xyz]
```

When we emit to room "user-A", both of User A's devices receive the message.

**Room naming convention**: Use the Gumnut user ID as the room name:

```python
room_name = session.user_id  # e.g., "550e8400-e29b-41d4-a716-446655440000"
```

This matches Immich's approach where clients join a room named with their user ID.

### 1.3 Session Tracking

To support disconnect cleanup and debugging, maintain a mapping of socket IDs to user IDs:

```python
# Maps socket ID -> user ID (for disconnect cleanup)
_sid_to_user: dict[str, str] = {}
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

5. Extract user_id from session:
   user_id = session.user_id

6. Join the socket to a user-specific room:
   await sio.enter_room(sid, user_id)

7. Store mapping for cleanup:
   - sid -> user_id (for disconnect cleanup)
   - Optionally: user_id -> [sids] (for multi-device support)

8. Emit on_server_version to the client (existing behavior)

9. Return True (implicit) to accept connection
```

---

## 3. Event System Design

### 3.1 Supported Events (Current Scope)

Starting with `on_upload_success`, but designed for future extension:

| Event Name | Payload | Sent To | Trigger |
|---|---|---|---|
| `on_upload_success` | `AssetResponseDto` | Asset owner | After upload completes |
| `on_server_version` | Version info | Connecting client | On connect (existing) |

### 3.2 Event Implementation Status

### Phase 1: Can Implement Now (No Backend Changes)

| Event | Payload | Web | Mobile | Notes |
|---|---|---|---|---|
| `on_upload_success` | `AssetResponseDto` | Yes | Legacy | photos-api thumbnails are synchronous |
| `AssetUploadReadyV1` | `SyncAssetV1` + `SyncAssetExifV1` | No | v2 sync | Emit alongside `on_upload_success` |
| `on_asset_delete` | `string` (assetId) | Yes | Yes | photos-api deletion is synchronous |
| `on_session_delete` | `string` (sessionId) | Yes | No | Sessions managed by immich-adapter |
| `on_server_version` | `ServerVersionResponseDto` | Yes | No | Sent on connect (existing) |

### Phase 2: Requires photos-api Event Channel

| Event | Payload | Web | Mobile | Notes |
|---|---|---|---|---|
| `on_person_thumbnail` | `string` (personId) | Page-specific | No | Cache busting after face detection |

### Not Applicable (Feature Not Supported)

These events require features that don't exist in photos-api:

| Event | Reason |
|---|---|
| `on_asset_trash` / `on_asset_restore` | photos-api hard-deletes (no soft-delete) |
| `on_asset_update` | Assets are immutable after creation |
| `on_asset_stack_update` | Stacks not implemented |
| `on_asset_hidden` | Asset visibility not supported |
| `on_notification` | Album sharing & job failures not implemented |
| `on_config_update` | Config management not implemented |
| `on_new_release` | Version checking service not implemented |

### Implementation Priority

1. **`on_upload_success` + `AssetUploadReadyV1`** - Core upload feedback for web and mobile
2. **`on_asset_delete`** - Timeline synchronization
3. **`on_session_delete`** - Security critical; forces web logout when session deleted

### 3.3 Mobile v2 Sync Protocol

Mobile clients using the v2 sync protocol listen to `AssetUploadReadyV1` instead of `on_upload_success`:

| Event | Payload | Notes |
|---|---|---|
| `AssetUploadReadyV1` | `{ asset: SyncAssetV1, exif: SyncAssetExifV1 }` | Compact format for real-time sync |

The mobile client batches these events and updates its local SQLite database immediately. Immich plans to deprecate `on_upload_success` in favor of `AssetUploadReadyV1`.

**Recommendation**: Emit both events on upload completion to support all clients.

### 3.4 Event Emission Interface

A single generic interface handles all event types using an enum for type safety:

```python
from enum import Enum
from typing import TypeAlias
from pydantic import BaseModel


class WebSocketEvent(Enum):
    """WebSocket events that can be emitted to clients."""

    # Phase 1: Can implement now
    UPLOAD_SUCCESS = "on_upload_success"
    ASSET_UPLOAD_READY_V1 = "AssetUploadReadyV1"
    ASSET_DELETE = "on_asset_delete"
    SESSION_DELETE = "on_session_delete"
    SERVER_VERSION = "on_server_version"

    # Phase 2: Requires photos-api event channel
    PERSON_THUMBNAIL = "on_person_thumbnail"


# Payload types: Pydantic models, strings, or lists of strings
EventPayload: TypeAlias = BaseModel | str | list[str] | None


async def emit_event(event: WebSocketEvent, user_id: str, payload: EventPayload = None) -> None:
    """
    Emit a WebSocket event to all of a user's connected clients.

    Args:
        event: The event type (from WebSocketEvent enum)
        user_id: The Gumnut user ID (room name)
        payload: Event data - a Pydantic model (auto-serialized), string, list of strings, or None
    """
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    await sio.emit(event.value, data, room=user_id)
```

Usage:

```python
# Pydantic model payload (auto-serialized)
await emit_event(WebSocketEvent.UPLOAD_SUCCESS, user_id, asset_dto)

# String payload
await emit_event(WebSocketEvent.ASSET_DELETE, user_id, asset_id)

# List of strings payload
await emit_event(WebSocketEvent.ASSET_TRASH, user_id, asset_ids)

# No payload
await emit_event(WebSocketEvent.CONFIG_UPDATE, user_id)
```

---

## 4. Implementation Structure

### 4.1 File: `routers/api/websockets.py`

```python
# Proposed structure

import logging
from enum import Enum
from http.cookies import SimpleCookie
from typing import TypeAlias

import socketio
from pydantic import BaseModel

from config.settings import get_settings
from services.session_store import get_session_store

logger = logging.getLogger(__name__)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    engineio_version=4,
)
socket_app = socketio.ASGIApp(socketio_server=sio, socketio_path="/")


class WebSocketEvent(Enum):
    """WebSocket events that can be emitted to clients."""

    # Phase 1: Can implement now
    UPLOAD_SUCCESS = "on_upload_success"
    ASSET_UPLOAD_READY_V1 = "AssetUploadReadyV1"
    ASSET_DELETE = "on_asset_delete"
    SESSION_DELETE = "on_session_delete"
    SERVER_VERSION = "on_server_version"

    # Phase 2: Requires photos-api event channel
    PERSON_THUMBNAIL = "on_person_thumbnail"


EventPayload: TypeAlias = BaseModel | str | list[str] | None


# Maps socket ID -> user ID (for disconnect cleanup)
_sid_to_user: dict[str, str] = {}


def _extract_session_token(environ: dict) -> str | None:
    """
    Extract session token from Socket.IO connection environment.

    Checks in order:
    1. x-immich-user-token header (mobile)
    2. Authorization: Bearer header (mobile alt)
    3. immich_access_token cookie (web)
    """
    # Check mobile header (HTTP_ prefix, dashes become underscores)
    if token := environ.get("HTTP_X_IMMICH_USER_TOKEN"):
        return token

    # Check Bearer token
    if auth := environ.get("HTTP_AUTHORIZATION", ""):
        if auth.lower().startswith("bearer "):
            return auth[7:]

    # Check cookie
    if cookie_str := environ.get("HTTP_COOKIE"):
        cookies = SimpleCookie()
        cookies.load(cookie_str)
        if "immich_access_token" in cookies:
            return cookies["immich_access_token"].value

    return None


@sio.event
async def connect(sid, environ):
    """
    Handle new WebSocket connection.

    Authenticates the client, joins them to their user room,
    and sends initial server version info.
    """
    logger.debug(f"WebSocket connect attempt - SID: {sid}")

    # Extract session token
    session_token = _extract_session_token(environ)
    if not session_token:
        logger.warning(f"WebSocket auth failed - no token found")
        return False  # Reject connection

    # Look up session in Redis
    try:
        session_store = await get_session_store()
        session = await session_store.get_by_id(session_token)
    except Exception:
        logger.exception("WebSocket auth failed - session lookup error")
        return False

    if not session:
        logger.warning(f"WebSocket auth failed - session not found")
        return False  # Reject connection

    # Join user room and track session
    user_id = session.user_id
    await sio.enter_room(sid, user_id)
    _sid_to_user[sid] = user_id

    logger.debug(f"WebSocket authenticated - SID: {sid}, User: {user_id}")

    # Send server version (existing behavior)
    version = get_settings().immich_version
    await emit_event(
        WebSocketEvent.SERVER_VERSION,
        sid,  # Send to this socket only, not the user room
        ServerVersionResponseDto(
            major=version.major,
            minor=version.minor,
            patch=version.patch,
        ),
    )


@sio.event
async def disconnect(sid):
    """Handle WebSocket disconnection."""
    user_id = _sid_to_user.pop(sid, None)
    logger.debug(f"WebSocket disconnected - SID: {sid}, User: {user_id}")


async def emit_event(event: WebSocketEvent, user_id: str, payload: EventPayload = None) -> None:
    """
    Emit a WebSocket event to all of a user's connected clients.

    Args:
        event: The event type (from WebSocketEvent enum)
        user_id: The Gumnut user ID (room name), or a socket ID for targeted emission
        payload: Event data - a Pydantic model (auto-serialized), string, list of strings, or None
    """
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    await sio.emit(event.value, data, room=user_id)
```

### 4.2 Usage from Assets Router

```python
# In routers/api/assets.py (after upload completes)

from routers.api.websockets import emit_event, WebSocketEvent

@router.post("/assets")
async def upload_asset(...):
    # ... upload logic ...

    # Notify connected clients
    await emit_event(WebSocketEvent.UPLOAD_SUCCESS, current_user.id, asset_response_dto)

    return asset_response_dto
```

```python
# In routers/api/assets.py (after delete completes)

@router.delete("/assets")
async def delete_assets(...):
    # ... delete logic ...

    # Notify connected clients for each deleted asset
    for asset_id in deleted_asset_ids:
        await emit_event(WebSocketEvent.ASSET_DELETE, current_user.id, asset_id)
```

---

## 5. Key Considerations

### 5.1 Error Handling

- If `emit_upload_success` is called for a user with no connected clients, it's a no-op (no error)
- Session lookup failures during connect result in connection rejection
- Emit failures should be logged but not block the upload response

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

1. **Unit tests for `_extract_session_token`**: Test cookie parsing, header extraction
2. **Integration tests for connect/disconnect**: Mock session store, verify room joining
3. **End-to-end tests**: Upload asset, verify websocket event received

---

## 7. Summary

The implementation:

1. **Builds on** existing Socket.IO infrastructure in `routers/api/websockets.py`
2. **Authenticates** websocket connections using the same session tokens as HTTP requests
3. **Joins** authenticated clients to a room named with their user ID
4. **Exposes** a single `emit_event(event, user_id, payload)` API with:
   - `WebSocketEvent` enum for type-safe event names
   - `EventPayload` type supporting Pydantic models, strings, string lists, or None
   - Automatic serialization of Pydantic models via `model_dump(mode="json")`
5. **Is extensible** - adding new events requires only a new enum value
