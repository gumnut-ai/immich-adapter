---
title: "Uvicorn Runtime Settings"
last-updated: 2026-09-26
---

# Uvicorn Runtime Settings

This reference records why the adapter does not run Uvicorn with all defaults and where to find the deployed values.

## Configuration owners

- `Dockerfile` owns the production command, bind address, `PORT` fallback, WebSocket implementation, keep-alive, concurrency, and backlog defaults.
- `.vscode/launch.json` owns the local debugger's WebSocket implementation.
- `pyproject.toml` owns the minimum Uvicorn version and its rationale.
- `README.md` shows a human-run production-style command.

Read those files for mutable values. Keeping a second settings table here would let documentation drift from the image that Render actually runs.

## Render bind contract

Render supplies `PORT`; the image command expands it at runtime and binds on all container interfaces. A process that listens only on loopback or ignores `PORT` is unreachable even when it starts successfully. `Dockerfile` also owns the health-check port expression, so bind and health behavior remain aligned.

## Container health check

The image health check requests `GET /api/server/ping` on
`http://localhost:${PORT:-8080}`. The endpoint is unauthenticated and returns
`{"res": "pong"}` when the adapter process is serving requests. It is a
liveness check only: the handler does not probe Redis or the Gumnut API.

Startup still depends on Redis. The application lifespan checks the configured
Redis connection before serving requests, so an unreachable Redis instance
prevents the application from starting even though the ping handler itself is
lightweight. Treat a passing ping as evidence that the process is alive, not as
confirmation that every upstream dependency is ready. `Dockerfile` owns the
health-check interval, timeout, start period, and retry count.

## WebSocket implementation

The adapter selects `websockets-sansio` explicitly. Uvicorn's `auto` selection previously routed Socket.IO traffic through the legacy `websockets` implementation and produced noisy `exception in shielded future` errors when peers closed. The Sans-I/O implementation avoids that failure mode and is also the transport used by the local debugger.

The minimum Uvicorn dependency is intentional: the selected implementation needs the keepalive behavior present at that floor. Do not lower it or switch protocols based only on a clean application test; exercise a real Socket.IO connect/disconnect path.

`tests/unit/config/test_uvicorn_ws_config.py` verifies that Uvicorn resolves `websockets-sansio` to the modern protocol and that the implementation does not import the legacy WebSocket tree. The production command, debugger setting, dependency floor, and their comments remain separate owners; verify them together when changing this protocol.

## HTTP connection tuning

Mobile and web clients make bursts of related requests and reuse connections. The production command therefore overrides Uvicorn's keep-alive, concurrency, and listen-backlog defaults. These values are deployment tuning, not architectural constants:

- `Dockerfile` is the source of truth.
- Environment variables can override the HTTP tuning without changing the image.
- Revisit values using request latency, connection pressure, memory, and rejection evidence; do not copy the current numbers into code or another reference.

## Verification

After changing runtime settings:

1. Run `uv run pytest tests/unit/config/test_uvicorn_ws_config.py`.
2. Run a production-style local server using the current command shape from `README.md`.
3. Connect and disconnect an Immich web or mobile client and check that Socket.IO traffic completes without shielded-future errors.
4. Verify the server listens on the supplied `PORT`, not only the documented fallback.

## macOS backlog diagnostic

A large listen backlog can exceed the host's `kern.ipc.somaxconn` and produce a warning locally. Check the current host limit with:

```bash
sysctl kern.ipc.somaxconn
```

For local testing, either choose a backlog within that limit or temporarily raise the host limit according to your machine-management policy. This is a host diagnostic, not a reason to change the production default without deployment evidence.
