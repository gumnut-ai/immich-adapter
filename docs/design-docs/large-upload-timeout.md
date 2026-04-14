---
title: "Large Upload Timeout"
status: active
created: 2026-04-13
last-updated: 2026-04-13
---

# Large Upload Timeout

## Problem

Uploads of large files (3+ GB videos) from Immich mobile clients fail consistently. The upload progresses to 75–89% completion and then the client disconnects, aborting the entire upload. Smaller files (under ~2.5 GB) succeed reliably.

## Root Cause

The Immich mobile client enforces a **60-second HTTP request timeout** for uploads. The adapter's streaming upload pipeline must relay the entire file to the upstream API within this window, or the client disconnects.

### Immich Client Timeouts

**Android** (`mobile/lib/infrastructure/repositories/network.repository.dart`):
```dart
OkHttpClientConfiguration(
  connectTimeout: Duration(seconds: 30),
  readTimeout: Duration(seconds: 60),
  writeTimeout: Duration(seconds: 60),
)
```

**iOS** (`mobile/ios/Runner/Core/URLSessionManager.swift`):
```swift
config.timeoutIntervalForRequest = 60
```

The upload endpoint (`POST /api/assets`) does not override these defaults for large files.

### Evidence

Render request logs (ingress layer) confirm the 60-second cutoff. Sizes and durations are rounded from representative samples; no user-identifying data is included.

| File | Size (approx.) | Duration | Status | Result |
|------|----------------|----------|--------|--------|
| Video | ~500 MB | ~13s | 201 | Success |
| Video | ~1 GB | ~25s | 201 | Success |
| Video | ~1.5 GB | ~33s | 201 | Success |
| Video | ~3.5 GB | ~61s | 499 | Client disconnected |
| Video | ~3.7 GB | ~60s | 502 | Client disconnected |

HTTP 499 is an ingress-layer status code ("client closed request") confirming the client terminates the connection, not the server. The 502 on the other request is the adapter's mapped error response (via `map_gumnut_error`) after detecting the `ClientDisconnect`.

### Pipeline Throughput

The streaming upload pipeline (client → adapter → upstream API) sustains approximately 50 MB/s (decimal megabytes). Actual throughput varies with network conditions and upstream processing overhead. Approximate transfer times at this rate:

- ~2.5 GB completes in ~50s — typically within the 60s window
- ~3 GB requires ~60s — borderline, may succeed or fail depending on conditions
- ~3.5 GB requires ~70s — consistently exceeds the window

Files in the 2.5–3 GB range are borderline; files above ~3 GB consistently fail. The pipeline cannot be made fast enough to handle arbitrarily large files within a fixed 60-second timeout.

## How the Streaming Upload Pipeline Works

The adapter streams uploads without buffering the entire file to disk. Three concurrent workers (one async task and two thread-pool workers) form a pipeline:

```
Immich client ──► _feed_chunks() ──► queue ──► _run_parser() ──► pipe ──► _sync_upload() ──► upstream API
                  (asyncio task,        (thread-pool worker,       (thread-pool worker,
                   reads request body)   multipart parser)          sync httpx POST)
```

1. **`_feed_chunks`** — Asyncio task that reads chunks from the Immich client's HTTP request body and enqueues them
2. **`_run_parser`** — Thread-pool worker that dequeues chunks, runs the multipart parser, and writes file data into a `StreamingPipe`
3. **`_sync_upload`** — Thread-pool worker that sends a sync httpx POST to the upstream API, with the pipe as the request body

When the client disconnects at 60 seconds, `_feed_chunks` raises `ClientDisconnect`, which propagates through the pipeline. The httpx POST to the upstream API is interrupted, the upstream API sees its own `ClientDisconnect`, and any in-progress S3 multipart upload is aborted.

### Current Timeout Configuration

| Component | Setting | Value |
|-----------|---------|-------|
| httpx client (adapter → upstream) | `connect` | 30s |
| httpx client (adapter → upstream) | `read` / `write` | 600s |
| Chunk queue | `get` timeout | 300s |
| Chunk queue | `put` stall timeout | 300s |
| Headers ready | `wait` timeout | 30s |

None of these are the bottleneck — the Immich client's 60-second timeout is the binding constraint.

## Workaround

Upload large files through the **Immich web app** instead of the mobile app. The web app uses `XMLHttpRequest` without setting a timeout, so uploads can run as long as needed. A 3.5 GB upload at ~50 MB/s takes ~70 seconds — well within the browser's unlimited timeout.

Limitations:
- The file must be accessible from the computer running the browser
- The browser tab must stay open during the upload

This only affects the mobile app's 60-second timeout. The upload pipeline through the adapter is the same for both clients.

## Options

### Option A: Chunked Upload Support

Implement a chunked upload endpoint that accepts file data in smaller pieces, each completing well within 60 seconds. The upstream Immich project has an in-progress branch (`feat/server-chunked-uploads`) designing this protocol.

**Pros:** Solves the problem for arbitrarily large files; resilient to network interruptions; aligns with upstream direction.
**Cons:** Requires implementing a new endpoint, temporary storage for in-progress chunks, and chunk assembly logic. Must match whatever protocol the Immich client implements. No timeline for when upstream ships this — building ahead of the client risks protocol divergence.

**Recommendation:** Monitor the `feat/server-chunked-uploads` branch. Implement adapter support when the Immich client ships chunked uploads.

### Option B: Accept-and-Forward

Accept the upload from the client (streaming to a temporary file), respond 201 immediately, then forward to the upstream API in the background.

**Pros:** Works with the current client — no client-side changes needed.
**Cons:** Requires temporary disk storage for 3+ GB files. The client receives a success response before the upstream API has processed the file, creating a window where the asset appears uploaded but isn't available. Error handling becomes complex (what if the background forward fails?).

### Option C: Pipeline Speed Optimization

Reduce pipeline latency to complete 3+ GB uploads within 60 seconds.

**Pros:** No API changes needed.
**Cons:** Would need ~63 MB/s sustained throughput for 3.73 GB. Current throughput is ~50 MB/s. The ceiling is set by network bandwidth between services and upstream API processing time, neither of which the adapter controls. Also doesn't scale — a 5 GB file would need ~85 MB/s.

## Recommendation

**Option A (chunked uploads)** is the right long-term fix, but we should wait for the upstream Immich client to ship the protocol before implementing it in the adapter. Monitor the `feat/server-chunked-uploads` branch for progress.

If large upload failures become a higher priority before upstream ships, Option B (accept-and-forward) is a viable interim fix, though it adds operational complexity.

## Related Issues

- Immich upstream branch: [`feat/server-chunked-uploads`](https://github.com/immich-app/immich/tree/feat/server-chunked-uploads)
- Immich PR [#27237](https://github.com/immich-app/immich/pull/27237): Removed `timeoutIntervalForResource = 300` on iOS but left the 60-second `timeoutIntervalForRequest`
- Immich PR [#27399](https://github.com/immich-app/immich/pull/27399) (on chunked uploads branch): "fix(mobile): low upload timeout on android"
