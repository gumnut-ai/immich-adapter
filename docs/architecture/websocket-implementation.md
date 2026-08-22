---
title: "WebSocket Implementation"
last-updated: 2026-08-21
---

# WebSocket Implementation

The adapter mounts a Socket.IO ASGI application at `/api/socket.io`. It provides best-effort realtime hints alongside the authoritative HTTP and sync-stream paths.

For event names, payloads, client consumers, and exact triggers, read the [WebSocket Events Reference](../references/websocket-events-reference.md). `services/websockets.py::WebSocketEvent` and router emit call sites remain the code authority.

## Topology

`main.py` mounts the Socket.IO application created in `services/websockets.py`. Every accepted socket joins two rooms:

```text
user room:    one user, all connected sessions
session room: one adapter session, all sockets for that session
```

User-scoped events update every connected device owned by the user. Session-scoped events target one login, which allows session revocation to tell only the affected client to disconnect.

The service retains a socket-ID-to-room mapping for disconnect cleanup and observability. It runs in the application's asyncio event loop; room membership and emission are delegated to python-socketio.

The WebSocket transport underneath Socket.IO is selected by Uvicorn. See [Uvicorn Runtime Settings](../references/uvicorn-settings.md) for the protocol choice and deployment owners.

## Authentication

Immich clients send the same adapter session UUID used by HTTP requests:

| Client path | Credential location |
|-------------|---------------------|
| Web | `immich_access_token` cookie |
| Mobile | `x-immich-user-token` header |
| Alternate mobile/client | Bearer token |

The connect handler:

1. extracts a session UUID from the supported header/cookie precedence;
2. rejects a missing or malformed token;
3. loads the session from Redis and rejects an expired or unknown session;
4. joins the socket to the user and session rooms;
5. records the socket mapping;
6. emits the current server version to the connecting client.

The client never sends the encrypted backend JWT to Socket.IO. Session custody and refresh are described in [Session and Checkpoint Implementation](session-checkpoint-implementation.md).

## Emission interface

Callers use the public wrappers in `services/websockets.py`:

- `emit_user_event` — one event to a user's room;
- `emit_user_event_per_id` — concurrent one-ID events for wire contracts that require them;
- `emit_session_event` — one event to a session room.

Pydantic payloads are serialized with `model_dump(mode="json")`, so UUIDs, datetimes, and enums use their JSON forms. Callers should pass typed DTOs or the explicitly supported scalar/list shapes instead of pre-serializing arbitrary JSON.

Event payload and trigger details deliberately live in the [event reference](../references/websocket-events-reference.md). That reference and the affected router call site must move together when an event's timing changes.

## Delivery and failure semantics

Realtime emission is best-effort relative to the HTTP mutation: the emit helpers are awaited by their caller — at nearly all call sites this happens inside the request, so emission precedes the HTTP response, though the video-upload path deliberately defers it to a background task (see the event reference) — but `emit_user_event` and `emit_session_event` catch `SocketIOError`, log at warning with exception context, and return normally.

This boundary is load-bearing: a photo mutation that committed in the Gumnut API must not be reported as failed merely because a client disconnected during notification. Call sites must not add duplicate `try/except SocketIOError` wrappers.

A user with no connected sockets is a successful no-op. Payload construction errors occur before transport emission and remain ordinary adapter failures; they are not swallowed by the transport boundary.

## Client convergence

Web clients use realtime events for immediate UI refresh. Mobile clients also consume supported realtime sync payloads, but the normal sync stream and checkpoints remain the durable convergence path. A missed Socket.IO event therefore delays visibility; it does not change the source of truth.

Bulk writes that return no updated asset payload normally skip an extra read solely to manufacture a realtime DTO. Mobile sync and web optimistic updates cover those flows unless observed client behavior proves otherwise. The rationale and caller rules live in [Asset and Media Handling](../references/asset-and-media-handling.md#websocket-emission).

## Verification

`tests/unit/api/test_websockets.py` covers credential precedence, connection rejection/acceptance, room membership, disconnect cleanup, serialization, targeting, and swallowed transport failures. Runtime protocol configuration is pinned separately by `tests/unit/config/test_uvicorn_ws_config.py`.
