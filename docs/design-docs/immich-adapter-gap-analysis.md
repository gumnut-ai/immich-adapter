---
title: "Immich Adapter Gap Analysis"
status: active
created: 2026-04-15
last-updated: 2026-08-25
---

# Immich Adapter Gap Analysis

## Context

The adapter targets the Immich version pinned in `.immich-container-tag`, currently **v3.1.0**. Core upload, timeline, albums, people/faces, search, map markers, stacks, trash, video playback, the Immich web editor's crop/rotate/mirror edits (`/api/assets/{id}/edits` over Gumnut version chains), and mobile sync workflows are implemented. The remaining work is concentrated in features whose product model does not yet exist in the Gumnut API, a few adapter-only compatibility surfaces, and scaling limits caused by protocol mismatch.

This is an active prioritization record, not an endpoint catalog. Generated models and the running FastAPI application own the exact route/schema surface. Re-run `tools/validate_api_compatibility.py` for a new target rather than updating copied counts here.

## Evaluation rules

Each gap is evaluated on:

- whether current Immich web or mobile clients reach it;
- whether a benign empty read is honest or a fake mutation would mislead;
- whether the Gumnut API has the required durable model;
- whether the adapter can translate the behavior without introducing a second source of truth;
- user value relative to implementation and operating cost.

A route being present in OpenAPI does not prove a client uses it. Check the pinned upstream source and feature gates before prioritizing it.

## Live gaps

| Area | Current behavior | Dependency | Disposition |
|------|------------------|------------|-------------|
| Shared links | Compatibility stubs; mutations do not create durable public links | Gumnut API sharing/access model plus adapter | Revisit: high value, large auth/storage design |
| Tags | Empty/fake compatibility surface | Gumnut API tag hierarchy and asset relations plus adapter | Revisit after core data model |
| Reverse geocoding | Empty read; map markers themselves work | Geocoding provider/storage plus adapter | Revisit independently |
| Activities/comments | Unreachable without sharing | Sharing model first | Intentional until sharing exists |
| Memories writes | Read path is available; save/hide persistence is not | Gumnut API memory model plus adapter | Revisit |
| Partners and album sharing | Unsupported single-user deployment model | Cross-user authorization and sharing | Revisit as a larger product capability |
| Duplicates | Empty compatibility surface | Product dedup model | Intentional; Gumnut uses a different approach |
| Notifications | Empty/fake compatibility surface | Primarily delivery for unsupported features | Intentional until a supported feature needs it |
| Search gaps | Some specialized searches remain stubs or have bounded/filter limitations | Mixed | Close individually from observed client need |
| Libraries | Compatibility stubs | Gumnut uses one library per user | Intentional |
| Session lock/PIN | Compatibility stubs | Conflicts with delegated Clerk/OAuth model | Intentional |
| Admin/user management | Compatibility stubs | Clerk and Gumnut administration own users | Intentional |
| System configuration/metadata | Compatibility responses | Deployment/backend owned | Intentional except fields needed to keep clients usable |
| Jobs/queues | Compatibility stubs | Gumnut owns its task system | Intentional |
| API keys | Compatibility stubs; API-key authentication itself is supported | Gumnut API key-management surface plus adapter | Revisit for developer workflows |
| Custom asset metadata | Compatibility stubs | Gumnut metadata model plus adapter | Revisit |
| OCR | Compatibility stub/no-op sync family | Gumnut OCR data plus adapter | Revisit |
| Database backup/maintenance | Unreachable admin surfaces | Gumnut operations | Intentional |
| Plugins/workflows | Unsupported optional utility | Gumnut extension/task model | Intentional |
| Folder view | Empty compatibility surface | Folder/path product model | Revisit |
| Pagination at scale | Some Immich-shaped reads exhaust or rewalk cursor listings | Gumnut server-side sort/filter/count plus adapter | Close as scaling work |
| Stack-collapsed month counts | Raw counts can exceed rendered tiles | Stack-aware Gumnut aggregate | Revisit; cosmetic |
| Trash-aware search | Criterion-bearing search lacks a complete trash selector | Gumnut search filters plus adapter | Revisit with search work |

## Immich v3 feature-area decisions

The v3 retarget introduced feature families whose reachability was checked against the pinned clients:

- **Adaptive video streaming:** intentional gap. The adapter reports realtime transcoding disabled, so clients use direct video playback.
- **Integrity checks:** intentional gap. These are storage-administration workflows, not client photo workflows.
- **OAuth backchannel logout:** intentional gap. The adapter is not the OIDC relying party; user logout is served by the normal auth route.
- **Plugins/workflows:** intentional gap. The optional utility page is unsupported.
- **Calendar heatmap:** the user read is a benign empty compatibility response; the admin variant remains unsupported.
- **Album map markers:** closed. Album views use the implemented Gumnut coordinate query.

These decisions are product/architecture choices, not promises that every unsupported route must remain a stub forever. Reassess when a client feature gate or Gumnut capability changes.

## Priorities

### Close next

1. **Pagination/scaling** — remove full-library reads where the Gumnut API can expose the required sort/filter/count behavior.
2. **Memories write path** — complete only with a durable backend model.
3. **Search limitations** — address from observed client workflows rather than broad speculative parity.

### Revisit with product capability

- shared links, partners, and album sharing;
- tags;
- reverse geocoding;
- API-key management;
- custom metadata, OCR, and folder view.

### Intentional gaps

Administration, infrastructure, and extension surfaces stay unsupported where Clerk or the Gumnut platform owns the capability and normal clients do not reach the route.

## Stub behavior

Read-only compatibility routes may return an empty shape when that cleanly renders an unsupported feature. Mutation stubs must not claim durable success when nothing was stored. Prefer a clear unsupported response, but verify current web and mobile degradation before changing an existing response: customer-visible error behavior is a separate decision from documentation maintenance.

Server feature flags are the first line of defense for hiding unsupported UI. A stub is a compatibility tool, not an implementation.

## Version upgrades

Before changing the pinned Immich version:

1. update the coordinated version owners described in [Routes, DTOs, and Upstream Compatibility](../references/routes-dtos-and-upstream-compatibility.md#bumping-the-immich-version);
2. regenerate models;
3. run API compatibility validation;
4. inspect added/removed routes and generated required fields;
5. read the tagged upstream client/server source for behavior the schema cannot express;
6. re-evaluate this live gap set.

The historical v2-to-v3 comparison and clean-cut rationale remain in [Immich v3 API Change Analysis](immich-v3-api-changes.md).
