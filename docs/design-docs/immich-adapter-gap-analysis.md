---
title: "Immich Adapter Gap Analysis"
status: active
created: 2026-04-15
last-updated: 2026-04-15
---

# Immich Adapter Gap Analysis

## Context

The immich-adapter translates Immich API calls into Gumnut SDK calls, allowing unmodified Immich clients (web v2.7.5 and mobile) to work with Gumnut's Photos API. The adapter implements 192 HTTP endpoints across 30 modules, but many of these are stubs that return empty lists, hardcoded fake data, or 204 responses.

Today, the core photo workflow works: upload, browse timeline, organize into albums, manage people/faces, search, and sync to mobile. Beyond this core, most Immich features are stubbed. This document inventories every gap, assesses user impact, estimates the effort to close each one, and identifies whether the work is adapter-only or requires Gumnut Photos API changes.

### Current implementation summary

| Category | Endpoint count | Status |
|----------|---------------|--------|
| Fully implemented (real SDK calls) | ~75 | Assets, albums, people, faces, timeline, sync, OAuth, search (partial), sessions (partial) |
| Stubs (empty/fake responses) | ~117 | Tags, shared links, memories, map, stacks, activities, admin, server info, etc. |
| Total in adapter | ~192 | |
| Not routed (no adapter endpoint) | ~52 | Immich endpoints with no adapter route at all (e.g., asset edits, database backups, workflows, plugins, some auth/admin endpoints) |
| Total in Immich v2.7.5 spec | 244 | |

## Goals

1. **Complete inventory** of every gap between the adapter and Immich v2.7.5's API surface
2. **User impact assessment** for each gap — does it break workflows or is it invisible?
3. **Dependency classification** — adapter-only work vs. requires Photos API changes vs. both
4. **Effort estimates** in T-shirt sizes (S/M/L/XL) per gap
5. **Prioritized recommendations** — what to close, what's intentionally out of scope, what to revisit later

## Gap Inventory

### How to read this section

Each gap is documented with:

- **Current behavior**: What the stub returns today
- **User impact**: How this affects Immich client users (Critical / High / Medium / Low / None)
- **Dependency**: Where the implementation work lives (Adapter-only / Backend needed / Both)
- **Effort**: T-shirt size estimate (S / M / L / XL)
- **Recommendation**: Close / Intentional gap / Revisit later

User impact levels:
- **Critical** — Core workflow broken, users can't accomplish basic tasks
- **High** — Feature users expect to work doesn't; visible failure or confusion
- **Medium** — Feature gap is noticeable but has workarounds
- **Low** — Rarely used feature or gap is invisible to most users
- **None** — Admin/system feature irrelevant in Gumnut's architecture

---

### 1. Shared Links (9 endpoints)

Immich shared links let users create public URLs to share individual assets or albums with non-users, optionally with passwords and expiry dates.

**Current behavior**: All 9 endpoints return fake data (dummy IDs, dummy keys). Creating a shared link appears to succeed but produces a non-functional link.

**User impact**: **High** — Sharing photos externally is a core photo management use case. Users who try the sharing UI will see it "work" but links won't resolve. This is arguably deceptive since the stub returns success rather than an error.

**Dependency**: **Both** — Gumnut Photos API would need a shared/public access concept, plus the adapter needs to generate and serve shared link pages.

**Effort**: **XL** — Requires designing public access controls in the backend (authentication exemption, access scoping, expiry), storage for link metadata, a serving path for unauthenticated asset access, and adapter translation.

**Recommendation**: **Revisit later** — High user value but significant backend design work. Consider implementing after core gaps are closed.

---

### 2. Tags (9 endpoints)

Immich tags allow hierarchical labeling of assets (e.g., `vacation/2024/beach`). Tags can be bulk-applied to assets.

**Current behavior**: All 9 endpoints return empty lists or fake single-item responses. Tag operations appear to succeed but nothing persists.

**User impact**: **Medium** — Power users rely on tags for organization. Most casual users rely on albums instead. The Immich web UI shows the tags sidebar, which is always empty.

**Dependency**: **Both** — Gumnut Photos API needs a tagging model (tag CRUD, asset-tag associations). Adapter work is straightforward translation once the backend exists.

**Effort**: **L** — Backend needs a new entity type (tags), a many-to-many association with assets, CRUD endpoints, and hierarchical tag support. Adapter translation is M on its own.

**Recommendation**: **Revisit later** — Moderate user value. Consider once Gumnut's data model stabilizes.

---

### 3a. Map Markers (1 endpoint)

Immich has a map view showing photo locations on a world map based on GPS coordinates from EXIF data.

**Current behavior**: `GET /map/markers` returns an empty list. The map view in the Immich web UI shows a blank map with no markers.

**User impact**: **Medium** — Users with geotagged photos expect to browse by location. The map tab is visible in the UI but empty.

**Dependency**: **Both** — Gumnut Photos API needs to surface GPS coordinates from EXIF data (it may already store them but not expose them via the API). Adapter needs to translate location data to Immich's `MapMarkerResponseDto`.

**Effort**: **S–M** — If GPS data is already stored in the backend, exposing it is straightforward. Adapter translation is S.

**Recommendation**: **Close** — Good user value for geotagged photo libraries.

---

### 3b. Reverse Geocoding (1 endpoint)

Immich's reverse geocoding translates GPS coordinates into human-readable place names (city, state, country).

**Current behavior**: `GET /map/reverse-geocode` returns an empty list.

**User impact**: **Low** — Place names enhance the map and search experience but are not required for basic map functionality (markers work without them).

**Dependency**: **Both** — Requires integration with an external geocoding service (e.g., Nominatim, Google Geocoding API, or a local database like GeoNames). The backend would need to store resolved place names and provide a geocoding endpoint or background job.

**Effort**: **M–L** — External service integration, caching strategy for resolved locations, and potentially a background job to backfill existing geotagged assets.

**Recommendation**: **Revisit later** — Map markers (3a) provide the core value; reverse geocoding can be added independently.

---

### 4. Trash (3 endpoints)

Immich supports soft-delete with a configurable retention period. Trashed items can be restored or permanently emptied.

**Current behavior**: All 3 endpoints return `TrashResponseDto(count=0)`. Assets deleted through the adapter are hard-deleted immediately.

**User impact**: **High** — Users who accidentally delete photos have no recovery path. The Immich UI shows a "Trash" section that's always empty, and the "Restore" option does nothing. This is a data safety issue.

**Dependency**: **Both** — Gumnut Photos API does not currently support soft-delete (no `deleted_at` or trash model exists). The backend needs a soft-delete model with configurable retention, and the adapter needs to route delete calls through the soft-delete path and expose the trash listing.

**Effort**: **M** — Backend needs a soft-delete column, retention policy, and purge mechanism. Adapter needs to translate Immich's `force` parameter on delete (force=true → permanent, force=false → soft-delete) and implement trash listing/restore/empty endpoints.

**Recommendation**: **Close** — Data safety feature with high user value.

---

### 5. Download / Archive (2 endpoints)

Immich allows batch downloading multiple assets as a zip archive.

**Current behavior**: `POST /download/archive` returns an empty binary response. `POST /download/info` returns `totalSize=0` with no archives.

**User impact**: **Medium** — Users who want to export/download multiple photos at once can't. Individual asset downloads work (via the asset original endpoint), but bulk export is broken.

**Dependency**: **Adapter-only** — The adapter can fetch individual assets from the backend and zip them server-side. No backend changes needed.

**Effort**: **M** — Requires implementing streaming zip construction from multiple asset downloads, handling potentially large archives, and proper Content-Disposition headers.

**Recommendation**: **Close** — Pure adapter work with clear user value.

---

### 6. Stacks (7 endpoints)

Immich stacks group related photos (e.g., burst shots, HDR series, RAW+JPEG pairs) with a primary asset representing the group.

**Current behavior**: All 7 endpoints return empty/fake responses. No stacking UI is functional.

**User impact**: **Low** — Stacking is a power-user feature. Most users don't manually stack photos. Some Immich features auto-create stacks (e.g., for live photos), but the adapter handles live photos differently.

**Dependency**: **Both** — Gumnut Photos API would need a grouping/stacking concept. The adapter translation is straightforward.

**Effort**: **L** — Backend needs a parent-child asset relationship model, group CRUD, and primary asset designation logic.

**Recommendation**: **Revisit later** — Low user impact. Consider when burst/HDR detection is added to Gumnut.

---

### 7. Activities / Comments (4 endpoints)

Immich activities allow users to add comments and reactions to shared albums.

**Current behavior**: All 4 endpoints return empty data or fake responses.

**User impact**: **Low** — Activities only work within shared albums, which are also not implemented. No user can reach this feature.

**Dependency**: **Both** — Requires shared albums to be implemented first (see gap #1). Then needs a comments/reactions model in the backend.

**Effort**: **M** — Straightforward CRUD once shared albums exist, but blocked by shared album implementation.

**Recommendation**: **Intentional gap** — Blocked by shared links/albums. Revisit if sharing is implemented.

---

### 8. Memories (8 endpoints)

Immich's "memories" feature auto-generates "On This Day" collections and similar nostalgia-based groupings.

**Current behavior**: All 8 endpoints return empty lists. The memories section in the Immich UI is empty.

**User impact**: **Medium** — "On This Day" is a popular engagement feature in photo apps. Users opening the app expect to see memories if they have photos from previous years.

**Dependency**: **Both** — Gumnut Photos API would need a memory generation system (likely a background job that queries assets by date). Adapter translation is straightforward.

**Effort**: **L** — Backend needs date-based query logic, memory entity CRUD, and a generation mechanism (could be a Celery task). Adapter adds S overhead.

**Recommendation**: **Revisit later** — Nice engagement feature but requires non-trivial backend work.

---

### 9. Partners (5 endpoints)

Immich partners allow two users to share their entire libraries with each other.

**Current behavior**: All 5 endpoints return empty lists or fake data.

**User impact**: **Medium** — Families/couples who want mutual library access can't use this feature. This is a key differentiator for self-hosted photo management.

**Dependency**: **Both** — Gumnut Photos API needs a library sharing / partner access model with cross-user authorization. This is a significant permission model change.

**Effort**: **XL** — Requires cross-user access control in the backend, a partner relationship model, shared asset visibility rules, and adapter translation for partner-filtered views.

**Recommendation**: **Revisit later** — High value for family use cases but requires deep authorization model changes.

---

### 10. Duplicates (3 endpoints)

Immich detects duplicate photos using perceptual hashing and lets users resolve them.

**Current behavior**: All 3 endpoints return empty lists. The duplicates page in the Immich UI is empty.

**User impact**: **Low** — Duplicate detection is a background optimization feature. Most users don't actively manage duplicates unless prompted.

**Dependency**: **Both** — Gumnut may handle deduplication differently (e.g., at upload time via checksums — see `docs/design-docs/checksum-support.md`). Surfacing duplicate candidates requires perceptual hash comparison in the backend.

**Effort**: **L** — Backend needs perceptual hashing (e.g., pHash), similarity matching, and a duplicate candidate API. Adapter translation is S.

**Recommendation**: **Intentional gap** — Gumnut's upload-time deduplication approach is different from Immich's post-hoc detection. Revisit if users request post-upload duplicate management.

---

### 11. Notifications (6 user + 3 admin endpoints)

Immich has an in-app notification system for events like album sharing invitations, new memories, etc.

**Current behavior**: All endpoints return empty lists or fake data.

**User impact**: **Low** — Notifications support other features (sharing, memories) that are also not implemented. No notification-worthy events exist today.

**Dependency**: **Both** — Backend needs a notification storage and delivery system. Adapter needs to translate notification types.

**Effort**: **M** — Straightforward once the triggering features exist, but blocked by those features.

**Recommendation**: **Intentional gap** — Notifications are a delivery mechanism for other features. Implement alongside the features that generate notifications (sharing, memories).

---

### 12. Search Gaps (6 stub endpoints within search module)

The search module has 3 real implementations (metadata, smart, person) and 5 stubs.

| Endpoint | Current behavior | Impact |
|----------|-----------------|--------|
| `GET /search/explore` | Empty list | **Low** — Curated categories, rarely used |
| `POST /search/large-assets` | Empty list | **Low** — Storage management tool |
| `GET /search/places` | Empty list | **Medium** — Location search, tied to map/EXIF |
| `GET /search/suggestions` | Empty list | **Medium** — Autocomplete for search bar |
| `GET /search/cities` | Empty list | **Low** — City list for location filtering |
| `POST /search/random` | Empty list | **Low** — Random photo selection |

**Dependency**: Places/cities/suggestions need backend location data (same as map markers gap #3a). Random and explore could be adapter-only with existing asset APIs. Large-assets needs file size data.

**Effort**: **M** total for the adapter-implementable ones (random, explore). Location-based search is tied to the map markers gap (#3a).

**Recommendation**: **Close** random and explore (S each, adapter-only). Location-based search endpoints close when map markers (#3a) closes.

---

### 13. Libraries (8 endpoints)

Immich libraries represent import sources (e.g., local folders, external storage). Gumnut has a different library model.

**Current behavior**: All 8 endpoints return empty/fake data. The library management UI shows no libraries.

**User impact**: **Low** — Immich libraries are primarily a self-hosted concept (scan local folders). Gumnut handles library management differently. The Immich mobile app doesn't use libraries.

**Dependency**: **Both** — Would require mapping Gumnut's library model to Immich's library concept, which may not be a 1:1 fit.

**Effort**: **M** — Model mapping complexity. May require design work to determine the right abstraction.

**Recommendation**: **Intentional gap** — Gumnut's library model is architecturally different. Force-fitting Immich's library concept could create confusion.

---

### 14. Server Info (15 endpoints)

Most server info endpoints return hardcoded fake data (storage, statistics, features, config, theme).

**Current behavior**: Version endpoints return dynamic data from settings. Everything else is hardcoded: storage shows fake disk usage, statistics show zeros, features list doesn't reflect actual Gumnut capabilities, config has hardcoded values.

| Endpoint | Current behavior | Impact |
|----------|-----------------|--------|
| `GET /server/features` | Hardcoded feature flags | **Medium** — Clients use this to enable/disable UI features. Incorrect flags may show UI for non-existent features. |
| `GET /server/config` | Hardcoded config | **Medium** — OAuth config and trash settings are hardcoded. |
| `GET /server/storage` | Fake disk usage | **Low** — Cosmetic in admin panel |
| `GET /server/statistics` | All zeros | **Low** — Admin panel stats |
| `GET /server/about` | Fake tool versions | **Low** — About page |
| `GET /server/theme` | Empty CSS | **None** — No custom theme |
| `GET /server/license` | Fake license | **None** — Gumnut isn't licensed this way |
| Other (6) | Version info, ping | **None** — Already functional or cosmetic |

**Dependency**: **Both** for features/config (need to reflect actual Gumnut capabilities). **Adapter-only** for storage/statistics (could query backend and translate).

**Effort**: **S** — Most of these are about returning accurate data rather than implementing new functionality. The features endpoint is the most important: it should reflect what Gumnut actually supports so Immich clients hide unsupported UI elements.

> **Design decision —** The `GET /server/features` endpoint is the highest-value fix in this group. Currently, the adapter sets `duplicateDetection: true`, `map: true`, `reverseGeocoding: true`, `trash: true`, and `sidecar: true` — all for features that are not implemented. Setting these to `false` would cause Immich clients to hide the corresponding UI elements, reducing user confusion. This is a quick win.

**Recommendation**: **Close** the features endpoint (S, adapter-only). Revisit storage/statistics if admin features are needed.

---

### 15. Sessions (2 stub endpoints within sessions module)

The sessions module has 4 real implementations and 2 stubs.

| Endpoint | Current behavior | Impact |
|----------|-----------------|--------|
| `POST /sessions` | 204 no content | **Low** — For casting/new token generation |
| `POST /sessions/{id}/lock` | 204 no content | **Low** — Session lock for PIN-based security |

**Dependency**: **Adapter-only** for session creation. Lock requires PIN support which ties into auth.

**Effort**: **S** — Session creation is straightforward. Lock is more complex (ties into PIN authentication, which is a separate Immich feature).

**Recommendation**: **Intentional gap** — PIN-based session locking is not part of Gumnut's auth model (Clerk OAuth). Session creation for casting is niche.

---

### 16. Admin / User Management (13 endpoints)

Immich has admin endpoints for managing users, preferences, notifications, and email templates.

**Current behavior**: All 13 endpoints return fake data. User listing returns empty, creating users returns a fake response.

**User impact**: **None** — Gumnut manages users through Clerk, not through the Immich admin panel. These endpoints are for Immich's self-hosted admin workflow.

**Dependency**: **N/A** — Gumnut's user management is handled by Clerk.

**Effort**: **N/A**

**Recommendation**: **Intentional gap** — User management is handled by Clerk. Admin endpoints would conflict with Gumnut's auth model.

---

### 17. System Config (4 endpoints)

Immich system config controls server-wide settings (ML, FFmpeg, storage template, etc.).

**Current behavior**: All 4 endpoints return hardcoded/empty responses.

**User impact**: **None** — These are Immich self-hosted admin settings. Gumnut manages its own configuration.

**Dependency**: **N/A**

**Effort**: **N/A**

**Recommendation**: **Intentional gap** — Configuration is managed by Gumnut's backend, not exposed through the Immich API.

---

### 18. System Metadata (4 endpoints)

Immich system metadata tracks onboarding state, reverse geocoding setup, and version check state.

**Current behavior**: All 4 endpoints return stub responses.

**User impact**: **None** — Admin/system internals.

**Dependency**: **N/A**

**Effort**: **N/A**

**Recommendation**: **Intentional gap** — Internal system state not relevant to Gumnut.

---

### 19. Jobs / Queues (3 + 5 endpoints)

Immich job and queue endpoints manage background processing (thumbnail generation, ML inference, etc.).

**Current behavior**: Jobs endpoints are deprecated stubs. Queue endpoints are not implemented in the adapter.

**User impact**: **None** — Gumnut handles background processing through its own Celery workers. Users don't need to manage jobs.

**Dependency**: **N/A**

**Effort**: **N/A**

**Recommendation**: **Intentional gap** — Background processing is managed by Gumnut's backend.

---

### 20. API Keys (6 endpoints)

Immich API keys allow programmatic access without OAuth.

**Current behavior**: All 6 endpoints return fake data.

**User impact**: **Low** — API keys are a developer/power-user feature. All current access is through OAuth.

**Dependency**: **Both** — Gumnut would need an API key concept for the Photos API.

**Effort**: **M** — Backend needs API key generation, storage, and authentication. Adapter translation is S.

**Recommendation**: **Revisit later** — Low priority unless third-party integrations need non-OAuth access.

---

### 21. Faces — Create Endpoint (1 endpoint)

The faces module has 3 real implementations but the create endpoint is stubbed.

**Current behavior**: Returns a stub response. The SDK doesn't support face creation.

**User impact**: **Low** — Face creation is typically automated (ML-driven face detection). Manual face creation is rare.

**Dependency**: **Both** — Gumnut SDK needs a face creation method. Backend may already support it but the SDK hasn't exposed it.

**Effort**: **S** — Likely just SDK and adapter changes if the backend already supports face creation.

**Recommendation**: **Close** — Small effort, completes the faces module.

---

### 22. People — Merge (1 endpoint)

Person merge is listed as a stub in the adapter architecture doc.

**Current behavior**: The merge endpoint exists but returns an empty list without performing any work.

**User impact**: **Medium** — When face clustering creates separate people entries for the same person, users need merge to combine them. Without it, people management becomes tedious.

**Dependency**: **Adapter-only** — All required SDK calls already exist: `faces.list(person_id=...)` to enumerate a person's faces, `faces.update(face_id, person_id=target)` to reassign each face, and `people.delete(source_id)` to remove the source person. These are the same calls used by the existing `reassign_faces` and `delete_person` endpoints.

**Effort**: **S** — Implement merge as: for each source person, list all faces → reassign each to the target person → delete the source person.

**Recommendation**: **Close** — Important for people management UX, small effort, no backend work needed.

---

### 23. Album Sharing (3 endpoints within albums module)

Within the albums module, the user sharing endpoints (`PUT /albums/{id}/users`, `DELETE /albums/{id}/user/{userId}`, `PUT /albums/{id}/user/{userId}`) return 501.

**Current behavior**: Returns 501 "not supported" when attempting to share an album with another user.

**User impact**: **High** — Album sharing is a primary collaboration feature. Users who try to share albums see an explicit error.

**Dependency**: **Both** — Requires the partner/sharing infrastructure from gap #9, plus album-specific access control.

**Effort**: **XL** — Tied to the broader sharing/permissions work (gap #9).

**Recommendation**: **Revisit later** — Blocked by partner/sharing infrastructure.

---

### 24. Asset Metadata — Custom Key-Value (4 endpoints)

Immich supports custom key-value metadata on assets (`GET/PUT/DELETE /assets/{id}/metadata/{key}`).

**Current behavior**: The `GET /assets/{id}/metadata/{key}` endpoint exists as a stub that returns an empty response. The bulk `DELETE /assets/metadata` endpoint also returns nothing. The other metadata endpoints (`PUT /assets/{id}/metadata`, `DELETE /assets/{id}/metadata/{key}`) are stubs.

**User impact**: **Low** — Custom metadata is a power-user/integration feature. Most users don't interact with it directly.

**Dependency**: **Both** — Gumnut Photos API would need an arbitrary metadata store per asset.

**Effort**: **M** — Backend needs a key-value metadata model per asset. Adapter translation is S.

**Recommendation**: **Revisit later** — Low user demand. Consider when third-party integration support is prioritized.

---

### 25. Asset Edits (3 endpoints)

Immich supports saving and retrieving photo edits (non-destructive editing).

**Current behavior**: These endpoints (`GET/PUT/DELETE /assets/{id}/edits`) are not implemented in the adapter — no routes exist.

**User impact**: **Low** — Photo editing in Immich is limited. Most users edit in external apps.

**Dependency**: **Both** — Backend needs edit storage (likely a sidecar approach). Adapter translation is S.

**Effort**: **M** — Backend needs edit sidecar storage and retrieval.

**Recommendation**: **Revisit later** — Low priority.

---

### 26. Asset OCR (1 endpoint)

Immich supports OCR text extraction from photos (`GET /assets/{id}/ocr`).

**Current behavior**: The endpoint exists as a stub that returns an empty list.

**User impact**: **Low** — OCR is a convenience feature for searching text in photos.

**Dependency**: **Both** — Backend needs OCR pipeline integration (likely already has ML infrastructure).

**Effort**: **M** — Backend ML pipeline addition. Adapter is S.

**Recommendation**: **Revisit later** — Consider alongside search improvements.

---

### 27. Database Backups — Admin (5 endpoints)

Immich has admin endpoints for database backup management.

**Current behavior**: Not implemented in the adapter.

**User impact**: **None** — Gumnut manages its own database backups through infrastructure tooling.

**Dependency**: **N/A**

**Effort**: **N/A**

**Recommendation**: **Intentional gap** — Gumnut has its own backup strategy.

---

### 28. Maintenance — Admin (4 endpoints)

Immich maintenance endpoints for system health checks and install detection.

**Current behavior**: Not implemented in the adapter.

**User impact**: **None** — Gumnut has its own monitoring (Sentry, health checks).

**Dependency**: **N/A**

**Effort**: **N/A**

**Recommendation**: **Intentional gap** — Covered by Gumnut's own monitoring.

---

### 29. Workflows (5 endpoints)

Immich workflows allow defining automated processing pipelines.

**Current behavior**: Not implemented in the adapter.

**User impact**: **None** — This is a newer Immich feature not widely adopted. Gumnut has its own task processing system.

**Dependency**: **N/A**

**Effort**: **N/A**

**Recommendation**: **Intentional gap** — Gumnut's Celery-based task system serves this purpose.

---

### 30. Plugins (3 endpoints)

Immich plugin system for extending functionality.

**Current behavior**: Not implemented in the adapter.

**User impact**: **None** — Plugin system is Immich-specific infrastructure.

**Dependency**: **N/A**

**Effort**: **N/A**

**Recommendation**: **Intentional gap** — Gumnut has its own extension model.

---

### 31. Auth Gaps (within auth module)

The auth module has partial implementation. Key gaps:

| Endpoint | Current behavior | Impact |
|----------|-----------------|--------|
| `POST /auth/login` | Returns 403 (disabled) | **None** — OAuth is the auth method |
| `POST /auth/change-password` | Stub | **None** — Passwords managed by Clerk |
| `POST /auth/pin-code` (CRUD) | Not implemented | **Low** — PIN is for device lock |
| `POST /auth/admin-sign-up` | Not implemented | **None** — Admin setup via Clerk |

**Recommendation**: **Intentional gap** — Auth is handled by Clerk OAuth. Password and PIN endpoints don't apply.

---

### 32. View / Folders (2 endpoints)

Immich folder view shows assets organized by file system path.

**Current behavior**: Both endpoints return empty lists.

**User impact**: **Low** — Folder view is a secondary browsing method. Most users use timeline or albums.

**Dependency**: **Both** — Backend would need to expose upload path or folder metadata.

**Effort**: **M** — Depends on whether folder/path data is stored in the backend.

**Recommendation**: **Revisit later** — Low priority secondary navigation.

---

### 33. Video Playback (1 endpoint)

The video playback endpoint (`GET /assets/{id}/video/playback`) is used by Immich clients to stream video files.

**Current behavior**: Returns an empty HTTP 200 response. The code comment notes that CDN streaming worked for the Immich web client but crashes the iOS mobile client, so it was disabled.

**User impact**: **High** — Videos cannot be played on iOS mobile. Users see videos in their library but playback fails silently. This is a core media feature.

**Dependency**: **Adapter-only** — The backend serves video files; the issue is in the adapter's streaming/proxying logic for mobile clients.

**Effort**: **M** — Requires debugging the iOS crash (likely a streaming format or range-request issue) and implementing mobile-compatible video streaming.

**Recommendation**: **Close** — Core media feature with high impact on mobile users.

---

### 34. Performance Gaps (not endpoint-specific)

These are architectural limitations documented in `docs/architecture/adapter-architecture.md`:

**Load-all-then-paginate pattern**: People, albums, and asset statistics endpoints load the entire dataset into memory before paginating. For a library with 10,000+ people or 100,000+ assets, this doesn't scale.

**User impact**: **Medium** — Performance degrades with library size. Not visible to small libraries but will become a problem at scale.

**Dependency**: **Both** — Gumnut Photos API needs server-side sorting and filtering for people and albums (currently only cursor-based pagination without sort control). Adapter can then use true cursor pagination.

**Effort**: **L** — Backend API design for server-side sort/filter. Adapter refactoring for cursor-based people/album listing.

**Recommendation**: **Close** — Important for scaling. Track as a separate task.

---

## Priority Summary

### Tier 1: Close Now (high value, reasonable effort)

| Gap | Effort | Dependency | Rationale |
|-----|--------|------------|-----------|
| #14 Server features endpoint | S | Adapter-only | Quick win — hides unsupported UI elements |
| #22 People merge | S | Adapter-only | Completes people management UX |
| #21 Face create | S | Both (may be S) | Completes faces module |
| #12 Search random/explore | S | Adapter-only | Easy adapter-only work |
| #4 Trash / soft-delete | M | Both | Data safety feature |
| #5 Download / archive | M | Adapter-only | Pure adapter work, clear user value |
| #33 Video playback (mobile) | M | Adapter-only | Core media feature broken on iOS |

### Tier 2: Close Next (moderate value or effort)

| Gap | Effort | Dependency | Rationale |
|-----|--------|------------|-----------|
| #3a Map markers | S–M | Both | Good value if EXIF GPS data exists in backend |
| #34 Performance (pagination) | L | Both | Scaling requirement |
| #8 Memories | L | Both | Engagement feature |
| #2 Tags | L | Both | Power-user organization |

### Tier 3: Revisit Later (high effort or blocked)

| Gap | Effort | Dependency | Rationale |
|-----|--------|------------|-----------|
| #3b Reverse geocoding | M–L | Both | Map markers provide core value without it |
| #1 Shared links | XL | Both | High value but major backend work |
| #9 Partners | XL | Both | Family use case, deep auth changes |
| #23 Album sharing | XL | Both | Blocked by sharing infrastructure |
| #6 Stacks | L | Both | Low user demand |
| #20 API keys | M | Both | Developer feature |
| #24 Custom metadata | M | Both | Integration feature |
| #25 Asset edits | M | Both | Low user demand |
| #26 OCR | M | Both | Search enhancement |
| #32 Folder view | M | Both | Secondary navigation |

### Intentional Gaps (not planned)

| Gap | Rationale |
|-----|-----------|
| #7 Activities/Comments | Blocked by sharing |
| #10 Duplicates | Different dedup approach in Gumnut |
| #11 Notifications | Delivery mechanism for unimplemented features |
| #13 Libraries | Architecturally different model |
| #15 Session lock/PIN | Not part of Clerk auth model |
| #16 Admin/User mgmt | Handled by Clerk |
| #17 System config | Backend-managed |
| #18 System metadata | Internal state |
| #19 Jobs/Queues | Gumnut has own task system |
| #27 Database backups | Gumnut has own backup strategy |
| #28 Maintenance | Gumnut has own monitoring |
| #29 Workflows | Gumnut has own task processing |
| #30 Plugins | Gumnut has own extension model |
| #31 Auth (password/PIN) | OAuth via Clerk |

## Stub Behavior Recommendation

Many stubs currently return fake success responses (HTTP 200 with dummy data), which misleads users into thinking features work when they don't. For example, creating a shared link returns a fake response with dummy IDs — the user thinks it worked, but the link is non-functional.

**Recommended approach**: Stubs for user-facing mutation endpoints (create, update, delete) should return an appropriate error rather than fake success:

- **Read endpoints** (list, get): Return empty lists or 404 for entity lookups. This is generally harmless — users see "no items" rather than a broken feature.
- **Write endpoints** (create, update, delete): Return 501 Not Implemented with a clear message (e.g., `"Shared links are not yet supported"`). This is honest and prevents users from thinking data was saved.
- **Endpoints that disable Immich UI**: Some Immich client features check whether an endpoint returns success before showing UI elements. Returning 501 on these endpoints may cause the client to hide the feature entirely, which is the desired behavior for unimplemented features.

The `GET /server/features` fix (gap #14) is the primary mechanism for hiding unsupported UI. Switching write stubs to 501 is a complementary measure for features that the features endpoint doesn't control.

> **Design decision —** Changing stubs from fake-success to 501 is a low-risk improvement that should be done alongside the server features endpoint fix (gap #14). Together, they give users an honest picture of what the adapter supports. Read-only stubs can continue returning empty data since this is harmless. Before bulk-converting stubs to 501, test how Immich web and mobile clients display 501 errors to ensure they degrade gracefully — if clients show confusing errors or crash, a softer approach (e.g., 404 with a descriptive message) may be preferable for some endpoints.

## Newer Immich Version Considerations

The adapter targets Immich v2.7.5. Newer Immich releases may introduce:

- **New API endpoints** not present in v2.7.5 — these would require new stubs at minimum
- **Changed request/response schemas** — the model generator (`tools/generate_immich_models.py`) and API compatibility validator (`tools/validate_api_compatibility.py`) can detect these
- **New sync entity types** — the sync stream implementation would need new converters
- **Breaking client changes** — newer Immich clients may require endpoints or fields that the adapter doesn't provide, causing errors rather than graceful degradation

> **Design decision —** Before upgrading the target Immich version, run the API compatibility validator against the new spec to identify breaking changes. The `.immich-container-tag` file controls which web UI version is bundled, so upgrading the container tag without updating the adapter could break the web UI.

## Verification

This document's accuracy can be verified by:

1. **Running the API compatibility tool** against the full Immich v2.7.5 spec to confirm endpoint coverage:
   ```bash
   uv run tools/validate_api_compatibility.py \
     --immich-spec=https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json \
     --adapter-spec=http://localhost:3001/openapi.json
   ```
2. **Testing each stub endpoint** to confirm it returns the documented behavior
3. **Checking Gumnut Photos API capabilities** to verify dependency classifications (which gaps need backend work vs. adapter-only)
