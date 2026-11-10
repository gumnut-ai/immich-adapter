---
title: "WebSocket Implementation"
last-updated: 2026-11-10
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

User-scoped events update every connected device owned by the user. Session-scoped events target one login, which lets session revocation reach only the affected client's sockets (see [Session revocation](#session-revocation)). python-socketio logs room names at INFO, so the session room is named `session:<telemetry id>` (`config/telemetry.py::session_telemetry_id`) rather than the session's bearer token.

The service retains a socket-ID-to-room mapping for disconnect cleanup and observability. It runs in the application's asyncio event loop; room membership and emission are delegated to python-socketio.

The WebSocket transport underneath Socket.IO is selected by Uvicorn. See [Uvicorn Runtime Settings](../references/uvicorn-settings.md) for the protocol choice and deployment owners.

Socket.IO applies its own origin check independently of FastAPI HTTP CORS. The
server is configured with `cors_allowed_origins="*"` in
`services/websockets.py`, so the Socket.IO handshake's origin policy is not a
statement about ordinary HTTP routes. The connect handler still requires a
session token from one of the supported credential locations below. A
deployment that needs credentialed cross-origin browser sockets should replace
the wildcard with an explicit origin allowlist.

## Authentication

Immich clients send the same adapter session token used by HTTP requests:

| Client path | Credential location |
|-------------|---------------------|
| Web | `immich_access_token` cookie |
| Mobile | `x-immich-user-token` header |
| Alternate mobile/client | Bearer token |

The connect handler:

1. extracts a session token from the supported header/cookie precedence;
2. rejects a missing or malformed token;
3. loads the session from Redis and rejects an expired or unknown session;
4. joins the socket to the user and session rooms;
5. records the socket mapping;
6. loads the session again and rejects the socket if it was deleted in the meantime (see [Session revocation](#session-revocation));
7. emits the current server version to the connecting client.

The client never sends the encrypted backend JWT to Socket.IO. Session custody and refresh are described in [Session and Checkpoint Implementation](session-checkpoint-implementation.md).

## Emission interface

Callers use the public wrappers in `services/websockets.py`:

- `emit_user_event` — one event to a user's room;
- `emit_user_event_per_id` — concurrent one-ID events for wire contracts that require them;
- `emit_session_event` — one event to a session room;
- `close_session_sockets` — `on_session_delete` to a session room, carrying the session's public id rather than its token, then a server disconnect of every socket in that room.

Pydantic payloads are serialized with `model_dump(mode="json")`, so UUIDs, datetimes, and enums use their JSON forms. Callers should pass typed DTOs or the explicitly supported scalar/list shapes instead of pre-serializing arbitrary JSON.

Event payload and trigger details deliberately live in the [event reference](../references/websocket-events-reference.md). That reference and the affected router call site must move together when an event's timing changes.

## Session revocation

Logout, `DELETE /api/sessions` and `DELETE /api/sessions/{id}` delete the session from Redis and then call `close_session_sockets` for each deleted session. It emits `on_session_delete` first, so the web client can run its logout navigation, and then disconnects every socket in the session room. A disconnect removes the socket from all of its rooms, including the user room, so the deleted session stops receiving the user's events even if its client ignores `on_session_delete`; leaving only the session room would not.

Neither Immich client retries a server-initiated disconnect: web reconnects after the next login, and mobile builds a new socket when it next connects. A reconnect with the deleted token is rejected by the connect handler. Deleting all other sessions never targets the caller's own session, so the caller's sockets stay connected.

OAuth login creates sessions without a Redis TTL, so a session does not expire on its own while a socket is connected; a session created with an expiry would need its own disconnect.

The connect handler reads the session again after joining its rooms, so a socket that connects while its session is being deleted is either closed with the session room or refused.

The server uses python-socketio's default in-memory client manager, so revocation, like every emit, reaches only sockets connected to the process that handled the request, not sockets connected to another instance of the service.

## Delivery and failure semantics

Realtime emission is best-effort relative to the HTTP mutation: the emit helpers are awaited by their caller — at nearly all call sites this happens inside the request, so emission precedes the HTTP response, though the video-upload path deliberately defers it to a background task (see the event reference) — but `emit_user_event` and `emit_session_event` catch `SocketIOError`, log at warning with exception context, and return normally.

This boundary is load-bearing: a photo mutation that committed in the Gumnut API must not be reported as failed merely because a client disconnected during notification. Call sites must not add duplicate `try/except SocketIOError` wrappers.

A user with no connected sockets is a successful no-op. Payload construction errors occur before transport emission and remain ordinary adapter failures; they are not swallowed by the transport boundary.

## Client convergence

Web clients use realtime events for immediate UI refresh. Mobile clients also consume supported realtime sync payloads, but the normal sync stream and checkpoints remain the durable convergence path. A missed Socket.IO event therefore delays visibility; it does not change the source of truth.

Bulk writes that return no updated asset payload normally skip an extra read solely to manufacture a realtime DTO. Mobile sync and web optimistic updates cover those flows unless observed client behavior proves otherwise. The rationale and caller rules live in [Asset and Media Handling](../references/asset-websocket-emission.md#websocket-emission).

## Verification

`tests/unit/api/test_websockets.py` covers credential precedence, connection rejection/acceptance, room membership, disconnect cleanup, serialization, targeting, and swallowed transport failures. `tests/unit/api/test_session_ids.py` drives each revocation route against the real Socket.IO room manager and checks that a deleted session's sockets receive no later user events while the caller's do. Runtime protocol configuration is pinned separately by `tests/unit/config/test_uvicorn_ws_config.py`.
