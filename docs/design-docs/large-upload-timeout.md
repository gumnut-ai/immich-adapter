---
title: "Large Upload Timeout"
status: deprecated
superseded-by: ../architecture/adapter-architecture.md
created: 2026-04-13
last-updated: 2026-09-02
---

# Large Upload Timeout

> **Deprecated (2026-09-02):** This design record evaluated mobile upload
> failures and possible timeout remedies. It does not describe the current
> upload pipeline. See [Immich Adapter Architecture](../architecture/adapter-architecture.md)
> for the living forwarding and cancellation behavior; the content below is
> retained for the historical context, alternatives, and rationale.

## Problem

At the time of this investigation, uploads of large files from Immich mobile
clients could disconnect late in transfer, aborting the entire upload.

## Root-cause reassessment

The investigation correlated these failures with client-side timeout settings.
Those settings describe connection, read, write, or request inactivity behavior;
they do not establish a universal fixed total-upload deadline. The current
architecture documents the adapter's forwarding and cancellation behavior.

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

## Options

### Option A: Chunked Upload Support

Implement a chunked or resumable upload endpoint that accepts file data in
smaller pieces. Upstream Immich had an in-progress branch
(`feat/server-chunked-uploads`) exploring this protocol.

**Pros:** Solves the problem for arbitrarily large files; resilient to network interruptions; aligns with upstream direction.
**Cons:** Requires implementing a new endpoint, temporary storage for in-progress chunks, and chunk assembly logic. Must match whatever protocol the Immich client implements. No timeline for when upstream ships this — building ahead of the client risks protocol divergence.

This option was not shipped. No resumable-upload protocol is implemented in
the adapter.

### Option B: Accept-and-Forward

Accept the upload from the client (streaming to a temporary file), respond 201 immediately, then forward to the Gumnut API in the background.

**Pros:** Works with the then-current client — no client-side changes needed.
**Cons:** Requires temporary disk storage for large files. The client receives a
success response before the Gumnut API has processed the file, creating a window
where the asset appears uploaded but is not available. Error handling becomes
complex if the background forward fails.

This option was not shipped.

### Option C: Pipeline Speed Optimization

Reduce forwarding latency enough to avoid client-side disconnects.

**Pros:** No API changes needed.
**Cons:** The ceiling is set by network bandwidth between services and Gumnut API
processing time, neither of which the adapter controls. Improving throughput
alone would not provide a resumable failure boundary for arbitrarily large
files.

This option was not adopted as the upload design.

## Historical recommendation

The evaluation favored a client-aligned chunked protocol, but no chunked or
resumable implementation shipped. The adapter instead retained synchronous
buffered and streaming forwarding; their current behavior is documented in
[Immich Adapter Architecture](../architecture/adapter-architecture.md).

Accept-and-forward was not selected because it would acknowledge success before
the Gumnut API had accepted the asset and would require durable background error
handling.

## Upstream context

- Immich PR [#27237](https://github.com/immich-app/immich/pull/27237): Removed `timeoutIntervalForResource = 300` on iOS but left the 60-second `timeoutIntervalForRequest`
- Immich PR [#27399](https://github.com/immich-app/immich/pull/27399) (on chunked uploads branch): "fix(mobile): low upload timeout on android"
- Immich PR [#22385](https://github.com/immich-app/immich/pull/22385): as of 2026-09-02, an open, server-only resumable-upload change; it did not represent a shipped client protocol or an adapter commitment

## Outcome

**2026-09-02:** The proposed fixed-timeout remedies were retired. No
chunked/resumable or accept-and-forward proposal shipped. Separate streaming
forwarding, bounded backpressure, client-disconnect cancellation, and internal
HTTP 499 handling did ship; the living description is in [Immich Adapter
Architecture](../architecture/adapter-architecture.md). This record remains
for the original context, alternatives, and rationale.
