---
title: "Uvicorn Server Settings"
last-updated: 2026-06-03
---

# Uvicorn Server Settings Explained

## Overview

This document covers the uvicorn server settings used by `immich-adapter`:

- HTTP-tier settings tuned for iOS/Flutter client compatibility — `timeout-keep-alive`, `limit-concurrency`, `backlog`.
- The WebSocket protocol implementation choice — `--ws websockets-sansio`.

These settings control how uvicorn (the ASGI server running our FastAPI application) handles HTTP and WebSocket connections, which is critical for mobile clients that make rapid successive requests and for the Socket.IO sync stream that backs the live Immich web/mobile UIs.

For the generic definition of each flag, see the official [uvicorn settings reference](https://www.uvicorn.org/settings/). This doc is not a restatement of that page — it records the **non-default values we run and why** (mobile keep-alive matching, the legacy-`websockets` shielded-future leak, the `uvicorn[standard]>=0.44.0` pin, the macOS `somaxconn` gotcha), which the upstream reference does not cover.

The adapter launches uvicorn from the `Dockerfile` CMD. Each HTTP-tier value is overridable via an environment variable, with the defaults shown below:

```text
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --log-level ${LOG_LEVEL} \
  --ws websockets-sansio \
  --timeout-graceful-shutdown 60 \
  --timeout-keep-alive ${TIMEOUT_KEEP_ALIVE:-75} \
  --limit-concurrency ${LIMIT_CONCURRENCY:-200} \
  --backlog ${BACKLOG:-2048}
```

### Settings Summary

| Setting | Value | Override env var | Rationale |
|---------|-------|------------------|-----------|
| `--ws` | `websockets-sansio` | — | Avoid the legacy `websockets` shielded-future leak (see below). |
| `--timeout-graceful-shutdown` | `60` | — | Give in-flight requests time to finish on redeploy. |
| `--timeout-keep-alive` | `75` | `TIMEOUT_KEEP_ALIVE` | Match the ~75s keep-alive that iOS/Android HTTP clients use. |
| `--limit-concurrency` | `200` | `LIMIT_CONCURRENCY` | Cap concurrent connections; excess gets HTTP 503 (not a connection refusal). |
| `--backlog` | `2048` | `BACKLOG` | OS accept-queue depth; overflow is a connection refusal (not a 503). |

---

## ws (WebSocket protocol implementation)

### What It Is

`--ws` selects which WebSocket implementation uvicorn uses for the WebSocket transport that the ASGI app (Socket.IO via `python-engineio`, in our case) sits on top of.

### Setting We Use

```text
--ws websockets-sansio
```

### Why Not the Default `--ws auto`

`auto` picks an implementation based on what's installed: if the `websockets` package is present (it is — it's pulled in transitively), uvicorn selects `uvicorn.protocols.websockets.websockets_impl:WebSocketProtocol`. That module imports `WebSocketServerProtocol` from `websockets.server`, which since `websockets` 14.0 is a deprecated lazy-import alias for `websockets.legacy.server.WebSocketServerProtocol` — the legacy class.

The legacy class implements its receive loop with `asyncio.shield(self.transfer_data_task)` so explicit close frames aren't cancelled by user-side timeouts. When a peer closes (gracefully or via keepalive timeout), the shielded task raises `ConnectionClosedError` / `ConnectionClosedOK`. The library's surrounding lifecycle doesn't always observe that exception before the task is GC'd, and asyncio logs `"exception in shielded future"` at ERROR.

In production this surfaced as ~70 Sentry events/day (close codes 1000 / 1005 / 1011) attributed to `logger=asyncio` with `mechanism=logging`.

### What `websockets-sansio` Does Differently

`websockets-sansio` uses the modern sans-I/O `websockets.server.ServerProtocol`. Its receive loop has no `asyncio.shield`; close frames flow out naturally and there are no shielded-future leaks.

The ASGI WebSocket scope/receive/send contract is identical between the two impls, so consumers (`python-engineio`, `python-socketio`, our Socket.IO handlers) don't need any code changes.

### Why We Require uvicorn ≥ 0.44.0

The sansio impl shipped in uvicorn 0.35.0, but it didn't gain WebSocket keepalive pings until **0.44.0**. Without keepalive pings the server would stop probing dead peers, which would silently regress the close-code 1011 ("keepalive ping timeout") detection that the legacy impl was doing for us. The pyproject.toml constraint `uvicorn[standard]>=0.44.0` exists for this reason — do **not** loosen it without first confirming the sansio impl in the target version still emits pings. uvicorn 0.42.0 also fixed several sansio bugs we want to be on the safe side of.

### Why We Don't Use `--ws wsproto`

`wsproto` is a different sans-I/O library entirely. We have no operational reason to switch libraries; staying on `websockets` (modern API) keeps the dependency graph simple and the failure modes well-known.

### Verifying the Change

- Static: `tests/unit/config/test_uvicorn_ws_config.py` asserts the setting resolves to the modern protocol class.
- Runtime: post-deploy, watch Sentry for asyncio `"exception in shielded future"` ERROR records (close codes 1000 / 1005 / 1011) over a 48h window — they should stop appearing.

---

## timeout-keep-alive

The number of seconds uvicorn keeps an idle HTTP connection open before closing it (uvicorn default: 5). We set **75**.

HTTP keep-alive lets a client reuse one TCP connection for multiple requests. If the server's keep-alive window is shorter than the client's, the client tries to reuse a connection the server has already closed and sees errors like `"Connection closed before full header was received"`.

Mobile HTTP clients drive the value: iOS `URLSession` and Android `OkHttp` both default to roughly **75 seconds** of keep-alive. Setting `timeout-keep-alive=75` to match avoids the truncated-reuse errors and the cost of a fresh TCP (and TLS) handshake on every request — handshakes are especially expensive on cellular networks.

The shorter the timeout, the fewer idle connections held open (less memory), at the cost of more handshakes and more connection-reuse errors for mobile clients.

---

## limit-concurrency

Maximum number of concurrent connections uvicorn will handle (uvicorn default: `None`, i.e. unlimited). We set **200**.

When the limit is reached, uvicorn rejects new requests with `HTTP/1.1 503 Service Unavailable` and a `Retry-After` header — a hard cap with no queuing. This protects the process from resource exhaustion under load: excess traffic fails fast and explicitly (503) while accepted requests keep processing normally (graceful degradation) instead of every request slowing down.

Raise it (via the `LIMIT_CONCURRENCY` env var) if legitimate traffic is being rejected with 503s; lower it if the process is running out of memory or saturating CPU.

---

## backlog

The maximum number of fully-established connections that can wait in the socket's OS-level accept queue before uvicorn calls `accept()` on them (uvicorn default and our value: **2048**). It maps to the `backlog` argument of the socket `listen()` call.

When the accept queue is full, the OS rejects or drops new connections and the client sees **"Connection refused"** or a timeout — this happens *before* uvicorn ever sees the connection. The effective queue depth is `min(backlog, os_somaxconn)`, so the OS limit can cap it (see the macOS gotcha below).

Raise it (via the `BACKLOG` env var) if clients report "Connection refused" during connection bursts and the OS limit allows it.

### Backlog vs limit-concurrency

These cap two different stages and fail differently:

| Setting | What It Limits | When It Applies | Failure Mode |
|---------|---------------|-----------------|--------------|
| `backlog` | Pending connections in the OS accept queue | Before `accept()` | Connection refused |
| `limit-concurrency` | Active connections inside the app | After `accept()` | HTTP 503 |

A connection passes through the `backlog` queue first, then counts against `limit-concurrency` once uvicorn accepts it.

---

## macOS development gotcha

The effective backlog is `min(uvicorn_backlog, os_somaxconn)`. On macOS, `kern.ipc.somaxconn` defaults to **128**, far below our `backlog=2048`, so a local dev server silently caps the accept queue at 128 and can refuse connections under a burst that production (Linux, with a much higher `somaxconn`) would absorb.

If you need the full backlog locally, raise the OS limit:

```bash
# Check current limit
sysctl kern.ipc.somaxconn      # macOS default: 128

# Raise it for the session (requires root)
sudo sysctl -w kern.ipc.somaxconn=2048
```
