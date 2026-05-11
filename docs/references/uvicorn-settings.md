---
title: "Uvicorn Server Settings"
last-updated: 2026-05-11
---

# Uvicorn Server Settings

`immich-adapter` runs FastAPI on uvicorn. Production and mobile-development
commands should use the same runtime choices unless a local workflow explicitly
requires otherwise.

## Current Settings

The Docker image starts the server with:

```bash
uvicorn main:app \
  --host 0.0.0.0 \
  --port ${PORT:-8080} \
  --log-level ${LOG_LEVEL:-info} \
  --ws websockets-sansio \
  --timeout-graceful-shutdown 60 \
  --timeout-keep-alive ${TIMEOUT_KEEP_ALIVE:-75} \
  --limit-concurrency ${LIMIT_CONCURRENCY:-200} \
  --backlog ${BACKLOG:-2048}
```

Use `uvicorn` directly for production-like local runs. The `fastapi` CLI does
not expose every flag we rely on.

## Quick Reference

| Setting | Value | Why |
|---------|-------|-----|
| `--ws` | `websockets-sansio` | Uses uvicorn's modern websockets implementation and avoids legacy close-task errors. |
| `--timeout-graceful-shutdown` | `60` | Gives in-flight requests and WebSocket cleanup time to finish during shutdown. |
| `--timeout-keep-alive` | `75` | Matches common mobile HTTP client keep-alive expectations and avoids unnecessary reconnect churn. |
| `--limit-concurrency` | `200` | Caps active app-level work so overload becomes controlled 503s instead of event-loop starvation. |
| `--backlog` | `2048` | Keeps the OS accept queue large enough for connection bursts. This is uvicorn's default, but we set it explicitly. |

## WebSocket Protocol

Always use:

```bash
--ws websockets-sansio
```

`--ws auto` selects an implementation based on installed packages. With the
current dependency graph it can route Socket.IO through uvicorn's legacy
`websockets` protocol implementation. That implementation uses a shielded
receive task; normal peer closes and keepalive timeouts can surface as noisy
asyncio `"exception in shielded future"` error logs.

`websockets-sansio` uses the modern sans-I/O implementation. The ASGI contract
presented to `python-engineio`, `python-socketio`, and the adapter's Socket.IO
handlers is the same.

The project requires a uvicorn version new enough for sans-I/O WebSocket
keepalive support. Do not loosen the uvicorn constraint without confirming the
selected version still sends keepalive pings from the sans-I/O implementation.

Verification:

- Static: `tests/unit/config/test_uvicorn_ws_config.py` checks the configured
  protocol class.
- Runtime: after deployment, Sentry should stop receiving asyncio
  `"exception in shielded future"` records for normal WebSocket closes.

## HTTP Keep-Alive

Use:

```bash
--timeout-keep-alive 75
```

Uvicorn's default is 5 seconds. That is too short for mobile clients that expect
to reuse idle HTTP connections for roughly a minute or more. When the server
closes an idle connection before the client expects it, the next request can fail
with closed-connection errors and then retry through a fresh TCP connection.

Trade-off: longer keep-alive holds more idle sockets open. Keep the value near
75 seconds unless production connection metrics show socket pressure.

## Concurrency Limit

Use:

```bash
--limit-concurrency 200
```

This is an application-level cap on active connections/tasks accepted by
uvicorn. When the limit is reached, uvicorn returns HTTP 503. That is preferable
to allowing unbounded active work to starve the event loop, especially during
mobile sync bursts or thumbnail-heavy sessions.

Tuning guidance:

- Increase only when CPU, memory, SDK/backend capacity, and event-loop latency
  show real headroom.
- Decrease if overload causes latency spikes before requests are rejected.
- Do not use this as the only overload control; endpoint-level limits and
  backend rate/concurrency controls still matter.

## Backlog

Use:

```bash
--backlog 2048
```

The backlog is the OS accept queue size for pending TCP connections. It operates
before uvicorn accepts a connection into application-level processing.

`backlog` and `limit-concurrency` protect different stages:

| Setting | Stage | Failure mode |
|---------|-------|--------------|
| `backlog` | OS accept queue before app processing | Connection refused or delayed by the OS |
| `limit-concurrency` | Uvicorn/application processing | HTTP 503 |

Uvicorn already defaults to 2048, but the adapter sets it explicitly so the
production command documents the intended burst tolerance.

## Render and Docker

Render sets `PORT` automatically for web services. The adapter must bind to
`0.0.0.0:$PORT` in production containers. Do not hard-code `3001` or another
development port in the Docker `CMD`.

Runtime flags are controlled by these environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORT` | `8080` | Container listen port fallback for local runs. Render supplies this in production. |
| `LOG_LEVEL` | `info` | Uvicorn log level. |
| `TIMEOUT_KEEP_ALIVE` | `75` | HTTP keep-alive timeout. |
| `LIMIT_CONCURRENCY` | `200` | Uvicorn active connection/task cap. |
| `BACKLOG` | `2048` | TCP accept queue size. |

See [Deploying to Render](../guides/deploying-to-render.md) for deployment
steps and local Docker smoke tests.

## Local Commands

Production-like local run:

```bash
uv run uvicorn main:app \
  --host 0.0.0.0 \
  --port 3001 \
  --ws websockets-sansio \
  --timeout-graceful-shutdown 60 \
  --timeout-keep-alive 75 \
  --limit-concurrency 200 \
  --backlog 2048
```

Mobile HTTPS development run:

```bash
uv run uvicorn main:app --reload --port 3001 \
  --host <local-lan-ip> \
  --ws websockets-sansio \
  --ssl-keyfile=key.pem \
  --ssl-certfile=cert.pem
```

For the full mobile setup, see
[Running with Immich Mobile](../guides/running-with-immich-mobile.md).

## Observability

Watch these signals when changing runtime settings:

- Sentry asyncio errors for WebSocket close handling.
- HTTP 503 rates from uvicorn concurrency limiting.
- Request latency during mobile sync and upload bursts.
- Open connection counts and memory usage.
- Container shutdown logs during deploys or restarts.

Useful local checks:

```bash
# Count established connections to a local adapter port.
netstat -an | grep ESTABLISHED | grep :8080 | wc -l

# Confirm the container health endpoint responds.
curl http://localhost:${PORT:-8080}/api/server/ping
```

## When to Revisit

Revisit these settings when:

- Uvicorn changes the default WebSocket implementation.
- The `websockets` or `python-socketio` dependency graph changes materially.
- Mobile clients report closed-connection errors.
- Production sees sustained 503s from concurrency limiting.
- Render instance sizes or expected concurrent mobile usage changes.
