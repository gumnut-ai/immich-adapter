---
title: "Uvicorn Runtime Settings"
last-updated: 2026-08-11
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

## WebSocket implementation

The adapter selects `websockets-sansio` explicitly. Uvicorn's `auto` selection previously routed Socket.IO traffic through the legacy `websockets` implementation and produced noisy `exception in shielded future` errors when peers closed. The Sans-I/O implementation avoids that failure mode and is also the transport used by the local debugger.

The minimum Uvicorn dependency is intentional: the selected implementation needs the keepalive behavior present at that floor. Do not lower it or switch protocols based only on a clean application test; exercise a real Socket.IO connect/disconnect path.

`tests/unit/config/test_uvicorn_ws_config.py` pins the production command, debugger configuration, dependency floor, and explanatory comments together.

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
