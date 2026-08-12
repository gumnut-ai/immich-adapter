---
title: "Immich v2.7.5 to v3 API Change Analysis"
status: deprecated
superseded-by: ../architecture/adapter-architecture.md
created: 2026-06-16
last-updated: 2026-08-11
---

# Immich v2.7.5 to v3 API Change Analysis

> **Deprecated —** This is the decision record for the adapter's completed clean-cut retarget to Immich v3. Current boundaries and translation behavior live in [Immich Adapter Architecture](../architecture/adapter-architecture.md); version upgrades and generated-model rules live in [Routes, DTOs, and Upstream Compatibility](../references/routes-dtos-and-upstream-compatibility.md).

## Context

The adapter originally targeted Immich v2.7.5. Immich v3 changed enough generated models and client behavior that treating regeneration as mechanical would have produced wire-compatible-looking code with runtime gaps.

The analysis compared the v2.7.5 OpenAPI specification with v3.0.0 release-candidate and GA specifications. The raw diff was noisy: annotation and generator changes made many schemas appear changed without changing JSON, while a smaller set of payload and client-behavior changes carried the real compatibility risk.

## Decision

Retarget the adapter to Immich v3 in one clean cut rather than maintaining a dual-version window.

The product was in alpha with a controlled client version, so the complexity of shims for removed endpoints and incompatible payload variants was not justified. The adapter would report the pinned v3 version, regenerate models, implement client-reached compatibility behavior, and explicitly classify unsupported feature families.

## Material findings

### Code-generation noise

Most schema churn came from reference flattening, format annotations, numeric type tightening, enum casing, and validation metadata. These changes mattered to Python symbols and validators but not necessarily to wire bytes. The lesson was to separate generated-code churn from behavior before scoping implementation.

### Breaking wire changes

The retarget had to account for several real payload changes:

- asset duration moved from an interval string to integer milliseconds on v3 asset models;
- album responses no longer carried the old inline owner/assets shape;
- asset response face/device fields changed;
- shared-link token/key behavior changed;
- removed asset-replace and legacy sync routes could not be preserved accidentally;
- new required response fields made hand-built stubs capable of failing only at runtime.

### Sync v2

The v3 client version-gates newer request types from the server version rather than negotiating per payload. The significant adapter mappings were:

- `SyncAssetV2`: integer-millisecond duration;
- `SyncAlbumV2`: no `ownerId`, requiring the separate owner-role `AlbumUserV1` stream;
- `SyncAssetFaceV2`: additional visibility/deletion fields;
- OCR, partner, and shared-album families: accepted explicit no-ops where the Gumnut data model had no equivalent.

The generated models own the exact current fields. The durable stream mapping and ownership rules are in [Sync Stream Architecture](../architecture/sync-stream-architecture.md) and [Immich Sync Wire Reference](../references/immich-sync-communication.md).

### New feature areas

The v3 surface added adaptive streaming, integrity checks, calendar heatmap, album map markers, OAuth backchannel logout, and plugin/workflow operations. Reachability mattered more than route count:

- album map markers were client-reached and small enough to implement faithfully;
- the user calendar heatmap received a benign compatibility response;
- HLS was gated off by the advertised server feature and direct video playback remained the supported path;
- admin integrity, backchannel logout, and optional plugin/workflow surfaces did not belong to the adapter's product boundary.

The current open set is maintained in [Immich Adapter Gap Analysis](immich-adapter-gap-analysis.md).

## Alternatives considered

### Dual v2/v3 compatibility window

Rejected. It would have retained removed endpoints, two duration representations, and divergent sync/album behavior for clients the deployment could already pin. The added testing surface did not buy a user migration path.

### Report an older server version and rely on V1 sync

Rejected as the final state. It could defer some sync changes, but would leave the bundled v3 client and generated API target inconsistent and hide required v3 behavior behind a false version.

### Treat the OpenAPI diff as the complete compatibility plan

Rejected. The specification cannot express which client views call a route, feature-gate behavior, array ordering, or how a response field is consumed. Tagged upstream source remained necessary.

## Outcome

Closed on **2026-08-11** after reverification against the default branch:

- the adapter pins Immich v3.1.0 in `.immich-container-tag` and the production image uses the coordinated version;
- generated v3 models are committed;
- v3 asset, album, face, owner-link, and sync request behavior is implemented;
- removed legacy routes were dropped rather than kept as shims;
- client-reached v3 map behavior is implemented and unsupported feature families are explicitly classified;
- linting, type checking, tests, and compatibility validation became the upgrade gates.

The retarget's current mechanics now live in evergreen architecture/reference documentation and code. This record retains the comparison method, clean-cut decision, alternatives, and compatibility lessons.
