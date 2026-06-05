---
title: "Uvicorn Server Settings"
last-updated: 2026-06-05
---

# Uvicorn Server Settings Explained

## Overview

This document covers the uvicorn server settings used by `immich-adapter`:

- HTTP-tier settings tuned for iOS/Flutter client compatibility — `timeout-keep-alive`, `limit-concurrency`, `backlog`.
- The WebSocket protocol implementation choice — `--ws websockets-sansio`.

These settings control how uvicorn (the ASGI server running our FastAPI application) handles HTTP and WebSocket connections, which is critical for mobile clients that make rapid successive requests and for the Socket.IO sync stream that backs the live Immich web/mobile UIs.

For the generic definition of each flag, see the official [uvicorn settings reference](https://www.uvicorn.org/settings/). This doc is not a restatement of that page — it records the **non-default values we run and why** (the mobile keep-alive floor, the legacy-`websockets` shielded-future leak, the `uvicorn[standard]>=0.44.0` pin, the macOS `somaxconn` gotcha), which the upstream reference does not cover.

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
| `--timeout-keep-alive` | `75` | `TIMEOUT_KEEP_ALIVE` | Hold idle HTTP connections well above mobile-client idle gaps (uvicorn default is 5s, far too short for mobile reuse). |
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

The number of seconds uvicorn keeps an idle HTTP connection open before closing it. The uvicorn default of 5s is far too short for mobile clients, which hold connections idle between bursts of requests and then try to reuse them: if the server closes the connection first, the client sees truncated-reuse errors (e.g. `"Connection closed before full header was received"`) and pays for a fresh TCP+TLS handshake on the next request — expensive on cellular.

`75` is the Gumnut-chosen value: long enough to sit above the idle gaps a mobile client leaves between request bursts (so connections survive to be reused), without holding idle sockets open indefinitely. It's not tied to a specific iOS/Android client default — treat it as a tunable floor, raised if reuse errors reappear, lowered if idle-connection memory becomes a concern.

---

## limit-concurrency

Caps concurrent connections (uvicorn default: unlimited); excess gets HTTP 503 rather than a connection refusal. See the Settings Summary table for the value and failure mode.

Raise it (via the `LIMIT_CONCURRENCY` env var) if legitimate traffic is being rejected with 503s; lower it if the process is running out of memory or saturating CPU.

---

## backlog

OS-level accept-queue depth; overflow is a connection refusal (not a 503), before uvicorn ever sees the connection. See the Settings Summary table for the value and failure mode. The effective depth is `min(backlog, os_somaxconn)`, so the OS limit can cap it (see the macOS gotcha below).

Raise it (via the `BACKLOG` env var) if clients report "Connection refused" during connection bursts and the OS limit allows it.

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
