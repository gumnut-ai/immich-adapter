---
title: "Immich Adapter Architecture"
last-updated: 2026-08-12
---

# Immich Adapter Architecture

## System boundary

`immich-adapter` presents the Immich v3 HTTP and Socket.IO contracts to unmodified Immich web, mobile, and integration clients, then translates supported operations to the Gumnut API.

```text
Immich clients
  -> FastAPI routes and Socket.IO
    -> authentication/session custody, DTO and ID translation
      -> Gumnut SDK and Gumnut API
```

The adapter owns protocol compatibility and short-lived translation state. It does not own photo storage, search indexes, user identity, or background media processing. Redis stores adapter sessions and per-session sync checkpoints; durable photo data remains in the Gumnut API.

The deployment assumes one Gumnut library per authenticated user. Immich library-management routes are compatibility stubs rather than a second library model.

## Compatibility invariants

The adapter cannot add Gumnut-specific endpoints or require client changes. A Gumnut capability with no Immich-shaped home therefore has to be represented by an existing Immich contract, an explicit compatibility stub, or a documented translation compromise.

The single-library assumption is more specific than a generic compatibility stub. Immich does not expose a library selector that the adapter can map to Gumnut, and `/api/libraries` is therefore an empty compatibility surface. Adapter calls omit `library_id` and rely on the Gumnut API to resolve the authenticated user's only library. Supporting multiple libraries would require one selection or fan-out decision shared across every route; fixing individual routes would create inconsistent authorization and data visibility.

## Request path

1. `AuthMiddleware` accepts an adapter session token or a Gumnut API key.
2. Session-token requests load the encrypted backend JWT from Redis; API-key requests bypass session lookup.
3. FastAPI validates generated Immich request models and route parameters.
4. The route calls typed Gumnut SDK methods when available and translates identifiers, filters, pagination, and response DTOs.
5. Global exception handling maps supported upstream failures into Immich's error envelope.
6. Asset/session mutations may emit Socket.IO events after the backend operation succeeds.

For session creation, credential custody, JWT refresh, checkpoint flow, and logout, read [Session and Checkpoint Implementation](session-checkpoint-implementation.md). For room membership and realtime failure behavior, read [WebSocket Implementation](websocket-implementation.md).

### Security decisions not visible in route behavior

The adapter currently has no CORS middleware because it serves the web bundle from its own origin. If cross-origin access is introduced, credentialed requests must use an explicit origin allowlist; never combine credentials with a wildcard origin.

Mobile OAuth has a deliberate security gap: providers that cannot redirect to `app.immich:///oauth-callback` are sent to the adapter's HTTPS `/api/oauth/mobile-redirect`, which then redirects to the custom scheme. Custom schemes have no OS-enforced app ownership, so another installed app could register the scheme and receive the authorization code. PKCE reduces the usefulness of an intercepted code but does not prevent interception. Universal Links or Android App Links would require publishing the mobile app's signing identity, making the hardening a mobile distribution decision rather than an adapter-only change.

The Gumnut API owns the OAuth client credentials and performs authorization URL creation and code exchange; the adapter passes its web and mobile redirect URIs through the SDK. Moving OAuth client registration to the adapter would require a coordinated backend and redirect-allowlist decision.

### Request observability

Sentry request context attaches the adapter session, public user identifier, route, and upstream failure metadata where available. Never put JWTs, API keys, or captured customer payloads in structured fields. Upstream response severity and aggregate degradation logging live in [Testing and Logging](../references/testing-and-logging.md).

## Translation boundaries

### Identifiers

The Gumnut API uses typed string identifiers; Immich exposes UUIDs. Helpers in `routers/utils/gumnut_id_conversion.py` perform the reversible encoding/decoding. Route and converter code must use those helpers rather than casting strings ad hoc.

A client-visible UUID in a mobile log therefore needs conversion before it can be looked up through the Gumnut API. The [sync architecture debugging note](sync-stream-architecture.md#debugging-immich-mobile-logs) gives the routing rule.

### Generated DTOs and upstream compatibility

`routers/immich_models.py` is generated from the pinned Immich OpenAPI spec. Hand-built response DTOs still require behavioral verification against the tagged upstream source because schemas cannot express ordering, client feature gates, or field consumption.

Route parameter, error-envelope, generated-model, upstream-source, and version-bump rules live in [Routes, DTOs, and Upstream Compatibility](../references/routes-dtos-and-upstream-compatibility.md).

### Asset and media translation

Asset converters translate Gumnut metadata, timestamps, checksums, media variants, dimensions, ratings, and visibility into Immich response models. Several fields are not identity mappings:

- capture-time and modified-time fallbacks differ;
- Immich's local datetime transport can carry local wall-clock components with a fictitious UTC marker;
- outgoing checksums are base64 SHA-1 even though stronger hashes exist internally;
- thumbnail selection depends on the requested/display aspect ratio;
- video upload events may wait for a renderable derived image.

The canonical rules and source anchors are in [Asset and Media Handling](../references/asset-and-media-handling.md).

## Collection translation

The Gumnut API is cursor-paginated while several Immich routes expose numeric pages, full arrays, or aggregate counts. Routes use one of four shapes:

- exhaust and shape a bounded entity listing when the Immich response requires global sorting or totals;
- walk a cursor stream only to the requested numeric page plus lookahead;
- constrain an asset listing by a half-open date range;
- fetch one entity directly.

The exact patterns, caps, failure propagation, fan-out bounds, and bulk-write semantics live in [Pagination, Bulk Operations, and Concurrency](../references/pagination-bulk-and-concurrency.md). `routers/api/constants.py` owns mutable request caps.

### Offset translation limitation

A numeric page cannot preserve the stability of an opaque cursor when entities change between requests. Duplicate or skipped rows across page boundaries are therefore possible on offset-shaped Immich routes. This is a protocol mismatch, not a condition the adapter can fully remove.

## Timeline stack collapse

Immich expects the server to collapse a stack to one tile and attach the stack summary to that survivor. The adapter implements both halves for `GET /api/timeline/bucket` when `withStacked` is enabled:

1. read stack rows in bounded chunks;
2. resolve a live timeline cover;
3. retain that cover and remove other members;
4. emit the stack UUID and live-member count on the surviving row.

### Timeline cover vs. effective primary

`routers/utils/stack_conversion.py::resolve_effective_primary` owns the common selection policy. Stack listings run it across all members; timeline collapse runs it across live members through `select_timeline_cover`.

The divergence is deliberate. A trashed pinned primary can remain on a stack during the retention window. A stack listing can still show that user choice alongside live members, but using the trashed asset as the destructive timeline cover would remove every live member from the grid. The timeline therefore chooses a visible frame.

Failures degrade toward showing more photos: an unresolved stack leaves its frames uncollapsed and its summary absent. Whole-resource failure degrades the bucket the same way rather than turning the primary timeline into a 5xx.

Monthly bucket counts are not stack-collapsed because the Gumnut aggregate API has no stack-aware count. The count can overstate a month with bursts until the real bucket layout loads; fixing that requires backend aggregate support.

## Trash and restore

The adapter translates Immich soft-delete, restore, empty-trash, and permanent-delete operations into Gumnut API trash primitives.

- `DELETE /api/assets` selects soft or permanent deletion from `force`.
- Restore-all and empty-trash enumerate the entire trashed cohort before mutating it so cursor pagination does not move under the walk.
- Returned counts can be approximate because backend bulk acknowledgements do not report per-row transitions.
- Timeline/statistics requests select trashed state explicitly.
- Sync sends trash state in `deletedAt`; Socket.IO distinguishes trash, restore, and permanent delete.

`TRASH_RETENTION_DAYS` is a shared deployment contract surfaced to Immich clients. The adapter cannot discover the backend retention dynamically, so deployments must configure the same value on both sides.

## Sync and realtime

`POST /api/sync/stream` consumes the Gumnut events API, hydrates current entities, converts them to generated Immich sync models, and resumes independently per entity type. Parent-first upserts and child-first deletes protect the mobile client's local foreign keys. Details, derived streams, and recovery tradeoffs live in [Sync Stream Architecture](sync-stream-architecture.md); wire shapes live in [Immich Sync Wire Reference](../references/immich-sync-communication.md).

Socket.IO complements sync for immediate UI updates. It is best-effort: transport errors are logged and swallowed centrally so a successful backend mutation is not turned into a failed HTTP request. See [WebSocket Implementation](websocket-implementation.md) and [WebSocket Events Reference](../references/websocket-events-reference.md).

## Static web assets

The production image extracts the pinned Immich web build from the upstream server image and the FastAPI application serves it as a fallback outside `/api`. `.immich-container-tag`, `Dockerfile`, and `scripts/extract-immich-web.py` own the mechanics. Local extraction and Vite workflows live in [Running with Immich Web](../guides/running-with-immich-web.md).

This keeps the client and generated API target aligned without adding a separate production web service.

## Failure behavior

- Authentication failures reject the request before a Gumnut client is created.
- Generated DTO validation failures are adapter bugs and must not be converted into plausible fake data.
- Typed Gumnut errors are mapped centrally; rate limits are retried by the SDK and never forwarded as HTTP 429 to Immich clients.
- Per-item response endpoints translate failures only where the Immich wire contract requires per-ID results. Atomic bulk writes otherwise propagate upstream failure.
- Sync hydration failures that would advance past missing data terminate the stream without completion so the client cannot acknowledge a corrupt position.
- Realtime emit failures are non-fatal after a successful mutation.

The active list of unsupported or stubbed feature areas belongs in [Immich Adapter Gap Analysis](../design-docs/immich-adapter-gap-analysis.md), not in this architecture overview.
