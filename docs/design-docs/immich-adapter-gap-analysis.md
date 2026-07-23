---
title: "Immich Adapter Gap Analysis"
status: active
created: 2026-04-15
last-updated: 2026-07-23
---

# Immich Adapter Gap Analysis

## Context

The immich-adapter translates Immich API calls into Gumnut SDK calls, allowing unmodified Immich v3.0.3 clients (web and mobile) to work with the Gumnut API. The adapter exposes 212 HTTP operations across 30 route modules, but many of these are stubs that return empty lists, hardcoded fake data, or 204 responses.

Today, the core photo workflow works: upload, browse timeline, organize into albums, manage people/faces, search, and sync to mobile. Beyond this core, most Immich features are stubbed. This document inventories every gap, assesses user impact, estimates the effort to close each one, and identifies whether the work is adapter-only or requires the Gumnut API changes.

### Current implementation summary

| Category | Endpoint count | Status |
|----------|---------------|--------|
| Current adapter operations | 212 | 30 route modules; a mix of real SDK-backed endpoints and intentional stubs |
| Not routed (no adapter endpoint) | 42 | Immich v3.0.3 operations with no adapter route at all (e.g., asset edits, database backups, workflows, plugins, and some auth/admin endpoints) |
| Total in Immich v3.0.3 spec | 254 | |

## Goals

1. **Complete inventory** of every gap between the adapter and Immich v3.0.3's API surface, using v2.7.5 as the historical baseline
2. **User impact assessment** for each gap — does it break workflows or is it invisible?
3. **Dependency classification** — adapter-only work vs. requires the Gumnut API changes vs. both
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

**Dependency**: **Both** — the Gumnut API would need a shared/public access concept, plus the adapter needs to generate and serve shared link pages.

**Effort**: **XL** — Requires designing public access controls in the backend (authentication exemption, access scoping, expiry), storage for link metadata, a serving path for unauthenticated asset access, and adapter translation.

**Recommendation**: **Revisit later** — High user value but significant backend design work. Consider implementing after core gaps are closed.

---

### 2. Tags (9 endpoints)

Immich tags allow hierarchical labeling of assets (e.g., `vacation/2024/beach`). Tags can be bulk-applied to assets.

**Current behavior**: **Partially closed (interim workaround).** The immich-go import path is emulated: `PUT /api/tags` upserts a tag (deterministic synthetic id, recorded `id → value` in Redis via `services/tag_store.py`) and `PUT /api/tags/{id}/assets` "assigns" assets by appending the tag to each asset's **description** (idempotently). This unblocks `immich-go upload from-folder --tag <name>`, which previously panicked when the upsert stub returned `[]`. The remaining 7 endpoints (`GET /api/tags`, `POST /api/tags`, `GET`/`PUT`/`DELETE /api/tags/{id}`, `DELETE /api/tags/{id}/assets`, `PUT /api/tags/assets`) are still stubs, so there is no real tag entity — tags don't round-trip as tags, only as description text.

**User impact**: **Medium** — Power users rely on tags for organization. Most casual users rely on albums instead. The Immich web tags sidebar stays intentionally disabled (surfacing it would show a permanently empty list, since `GET /api/tags` returns `[]`); the interim workaround serves only bulk importers.

**Dependency**: **Both** — a real tag feature needs the Gumnut API to model tags (tag CRUD, asset-tag associations). The description-append workaround is adapter-only and does not migrate to real tags automatically when the backend lands.

**Effort**: **L** — Backend needs a new entity type (tags), a many-to-many association with assets, CRUD endpoints, and hierarchical tag support. Adapter translation is M on its own.

**Recommendation**: **Revisit later** — The interim import workaround is in place; a full tag feature waits on Gumnut's data model. When real tags land, replace the description-append emulation and consider a one-time migration of embedded `#`-tags.

---

### 3a. Map Markers (1 endpoint)

Immich has a map view showing photo locations on a world map based on GPS coordinates from EXIF data.

**Current behavior**: Implemented. `GET /map/markers` passes a world-wide `bbox` to `client.assets.list()` so the Gumnut API returns only geotagged assets, then returns up to 2000 markers (newest first). A scan bound remains only as a degraded-path safety net if the coordinate filter is unavailable. The `map` server-feature flag is on, so Immich web and mobile render the map view.

**Status**: **Closed** — adapter-side wire-up over the Gumnut API's coordinate filter. Reverse-geocoding (3b) remains the outstanding map-related gap.

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

**Current behavior**: **Closed** — `DELETE /api/assets` honors `force`, `/api/trash/restore*` and `/api/trash/empty` are wired to real backend trash operations, timeline/statistics respect `isTrashed`, sync surfaces `deletedAt`, and WebSocket events distinguish trash, restore, and permanent delete. `GET /api/server/config` also surfaces `trashDays` from `TRASH_RETENTION_DAYS`.

**User impact**: **None** — users can move assets to trash, restore them, empty trash, and see the configured retention window in the web UI.

**Dependency**: **Both** — the adapter implementation depends on backend soft-delete support and translates it into Immich's wire contract.

**Effort**: **M** — now shipped across delete flow, trash router, timeline/statistics, sync, WebSocket, and server config.

**Recommendation**: **Closed** — detailed implementation notes now live in `docs/design-docs/trash-soft-delete-adapter.md`.

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

**Dependency**: **Both** — the Gumnut API would need a grouping/stacking concept. The adapter translation is straightforward.

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

**Current behavior**: Read endpoints `GET /memories` and `GET /memories/{id}` synthesize OnThisDay memories at request time by querying the Gumnut API for assets captured on today's local month/day across the previous 30 years (one parallel call per year). Memory IDs encode `(user, year, month, day)` so they round-trip without persistence. The Immich web "On This Day" carousel renders correctly.

`GET /memories/statistics` remains a `total=0` stub. No upstream Immich client (web or mobile) calls it, so synthesizing a real count would burn round-trips for a value nobody reads.

Write endpoints (`POST /memories`, `PUT /memories/{id}`, `DELETE /memories/{id}`, `POST/DELETE /memories/{id}/assets`) remain no-op stubs. The memory viewer's save/hide/remove actions appear to succeed but don't persist; this is acceptable for the carousel-only goal and is flagged in `routers/api/memories.py`.

**User impact**: **Resolved for the read path** — the engagement feature is now visible. Persistence for save/hide is a follow-up.

**Dependency**: **Adapter only** for read; write would need a backend persistence layer (or an adapter-side store).

**Effort**: **S remaining** — Adding write persistence is the only piece left, and only if save/hide UX matters before another consumer needs durable memories.

**Recommendation**: **Revisit later** — the read path covers the carousel; revisit if/when save/hide UX is requested.

---

### 9. Partners (5 endpoints)

Immich partners allow two users to share their entire libraries with each other.

**Current behavior**: All 5 endpoints return empty lists or fake data.

**User impact**: **Medium** — Families/couples who want mutual library access can't use this feature. This is a key differentiator for self-hosted photo management.

**Dependency**: **Both** — the Gumnut API needs a library sharing / partner access model with cross-user authorization. This is a significant permission model change.

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

### 12. Search Gaps (4 stub endpoints within search module)

The search module has 5 real implementations (metadata, smart, person, random, explore) and 4 stubs.

| Endpoint | Current behavior | Impact |
|----------|-----------------|--------|
| ~~`GET /search/explore`~~ | Closed — cities (`exifInfo.city`) + recents (`createdAt`) groups derived from recent assets | — |
| `POST /search/large-assets` | Empty list | **Low** — Storage management tool |
| `GET /search/places` | Empty list | **Medium** — Location search, tied to map/EXIF |
| `GET /search/suggestions` | Empty list | **Medium** — Autocomplete for search bar |
| `GET /search/cities` | Empty list | **Low** — City list for location filtering |
| ~~`POST /search/random`~~ | Closed — uniform random sample via month-bucket counts | — |

**Dependency**: Per-endpoint breakdown:
- **Places/cities**: Need reverse geocoding for human-readable place names — tied to gap #3b, not #3a (GPS coordinates alone don't provide place names).
- **Suggestions**: In v2.7.5, `SearchSuggestionType` includes `country`, `state`, `city` (location-based, tied to #3b) and `camera-make`, `camera-model` (EXIF-based, potentially adapter-only if EXIF data is already in the backend).
- **Random/explore**: Closed — implemented adapter-only on existing asset APIs (random via month-bucket count sampling, explore via a recent-asset scan grouped by `metadata.city`).
- **Large-assets**: Needs file size data from backend.

**Effort**: **S** for the remaining adapter-implementable one (camera suggestions). Location-based search is tied to reverse geocoding (#3b).

**Recommendation**: Camera suggestions may be closeable independently (S, adapter-only if EXIF data available). Location-based endpoints close when reverse geocoding (#3b) closes.

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

Most server info endpoints return hardcoded fake data (storage, statistics, features, config).

**Current behavior**: Version endpoints return dynamic data from settings. `GET /server/features` is static but accurate. `GET /server/config` is still mostly hardcoded, but `trashDays` now reflects `TRASH_RETENTION_DAYS` at request time. Storage still shows fake disk usage, statistics still return zeros, and the about/license responses remain placeholders. (`GET /server/theme` was removed in Immich v3 and dropped from the adapter.)

| Endpoint | Current behavior | Impact |
|----------|-----------------|--------|
| `GET /server/features` | Static flags, accurate | **None** — Flags now reflect actual adapter capabilities. |
| `GET /server/config` | Mostly hardcoded; `trashDays` is dynamic | **Low** — Trash retention is accurate now; remaining fields are mostly cosmetic. |
| `GET /server/storage` | Fake disk usage | **Low** — Cosmetic in admin panel |
| `GET /server/statistics` | All zeros | **Low** — Admin panel stats |
| `GET /server/about` | Fake tool versions | **Low** — About page |
| `GET /server/theme` | **Removed in v3** — dropped from the adapter | **None** — Gone from the v3 spec |
| `GET /server/license` | Fake license | **None** — Gumnut isn't licensed this way |
| Other (6) | Version info, ping | **None** — Already functional or cosmetic |

**Dependency**: `GET /server/features` is **Adapter-only** — already closed. `GET /server/config` is now largely **Adapter-only** for the values we currently surface; `trashDays` already tracks `TRASH_RETENTION_DAYS`, but any future backend-owned settings would still need coordination. Storage/statistics are **Adapter-only** (could query backend and translate).

**Effort**: **S** — Most of these are about returning accurate data rather than implementing new functionality. The features endpoint was the highest-value fix in this group, and `trashDays` is now accurate as well.

> **Design decision —** The `GET /server/features` endpoint was the highest-value fix in this group. Previously the adapter set `duplicateDetection: true`, `map: true`, `reverseGeocoding: true`, `trash: true`, and `sidecar: true` for features that were not implemented. Those flags were flipped to `false` so Immich clients hide unsupported UI. As the adapter gained real implementations, `map` and `trash` were re-enabled; `reverseGeocoding`, `duplicateDetection`, and `sidecar` remain `false` because those capabilities are still stubbed or intentionally unsupported. `smartSearch`, `facialRecognition`, `search`, `oauth`, and `oauthAutoLaunch` remain `true` because they are backed by real implementations. Separately, `GET /server/config` now reads `trashDays` from settings instead of returning a hardcoded 30-day placeholder.

**Recommendation**: **Closed** for features and trash retention. Revisit storage/statistics and any remaining config fields only if the admin surface becomes important.

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

**Current behavior**: The 6 `/api/api-keys` *management* endpoints return fake data (key CRUD is still stubbed). However, **inbound `x-api-key` authentication is now supported**: `AuthMiddleware` reads the `x-api-key` header and forwards its value to the Gumnut API as the caller's credential, so a client such as immich-go can authenticate with a Gumnut API key (`apikey_...`). See `docs/guides/importing-with-immich-go.md`.

**User impact**: **Low** — API keys are a developer/power-user feature. Interactive access is through OAuth; headless clients can now use a Gumnut API key.

**Dependency**: Inbound auth was **Adapter-only** — the Gumnut API already validates `apikey_...` bearer tokens, so the adapter just forwards the header. Key *management* through the Immich UI remains **out of scope**: minting a key is a credential-management operation the Gumnut API only permits from a first-party browser session, and the adapter authenticates with a delegated OAuth token that cannot mint keys. Users mint keys in the Gumnut app instead, so the `/api/api-keys` CRUD stubs stay as-is.

**Effort**: Inbound auth was **S** (done). Wiring the CRUD endpoints is not planned (blocked by the first-party-session requirement above).

**Recommendation**: **Closed** for inbound `x-api-key` auth. The management-endpoint stubs are an intentional gap — key minting lives in the Gumnut app.

---

### 21. Faces — Create Endpoint (1 endpoint)

**Current behavior**: **Closed** — `POST /api/faces` is implemented in `create_face` (`routers/api/faces.py`). It converts the Immich asset and person UUIDs to Gumnut IDs, calls `client.faces.create(asset_id, bounding_box={x,y,w,h}, person_id)`, and returns the created face as an `AssetFaceResponseDto`. This backs Immich's "create a face on-the-fly" flow in the face tag editor (the client creates the person via `POST /people`, then draws the box via this endpoint). Required `gumnut-sdk >= 0.116.0`, which exposes the auto-generated `faces.create()` once the backend added the face-creation endpoint.

**User impact**: **Low** — Face creation is typically automated (ML-driven face detection). Manual face creation is rare. Without it, the on-the-fly flow created orphaned person records with no linked face/thumbnail.

**Dependency**: **Both** — required a backend face-creation endpoint (now present, exposed by the auto-generated SDK) plus the adapter translation.

---

### 22. People — Merge (1 endpoint)

Person merge is listed as a stub in the adapter architecture doc.

**Current behavior**: The merge endpoint exists but returns an empty list without performing any work.

**User impact**: **Medium** — When face clustering creates separate people entries for the same person, users need merge to combine them. Without it, people management becomes tedious.

**Dependency**: **Adapter-only** — All required SDK calls already exist: `faces.list(person_id=...)` to enumerate a person's faces, `faces.update(face_id, person_id=target)` to reassign each face, and `people.delete(source_id)` to remove the source person. These are the same calls used by the existing `reassign_faces` and `delete_person` endpoints.

**Effort**: **S** — Implement merge as: for each source person, list all faces (paginate fully via the SDK's async iterator) → reassign each to the target person → delete the source person only after all reassignments succeed. Partial failure handling: if a reassignment fails mid-merge, the source person should not be deleted (faces would be orphaned).

**Recommendation**: **Closed** — implemented in `merge_person` (`routers/api/people.py`) as a thin pass-through to `client.people.merge`, which atomically reassigns all faces, deletes the sources, and recalculates the primary's centroid embedding. Self-merge is rejected client-side with 400 to keep the Immich error shape stable; empty `ids` is a no-op.

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

**Dependency**: **Both** — the Gumnut API would need an arbitrary metadata store per asset.

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

**v3 (3.0) note**: The workflow model was restructured (`actions`/`filters` →
`steps`/`methods`/`trigger`) and the endpoints reshaped; the intentional-gap
verdict is unchanged. See *Immich v3 New Feature Areas — Scope Decisions* for the
v3 endpoint list and client reachability.

---

### 30. Plugins (3 endpoints)

Immich plugin system for extending functionality.

**Current behavior**: Not implemented in the adapter.

**User impact**: **None** — Plugin system is Immich-specific infrastructure.

**Dependency**: **N/A**

**Effort**: **N/A**

**Recommendation**: **Intentional gap** — Gumnut has its own extension model.

**v3 (3.0) note**: The plugin endpoints were reshaped (`/plugins/methods`,
`/plugins/templates`); the intentional-gap verdict is unchanged. See *Immich v3
New Feature Areas — Scope Decisions* for the v3 endpoint list and client
reachability.

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

**Current behavior**: Implemented. Streams the asset's `original` variant from CDN via `_retrieve_and_stream_variant`, forwarding the client's `Range` header for seeking. `stream_from_cdn` advertises `Accept-Ranges: bytes` on the initial 200 response so iOS AVPlayer treats the source as seekable, which addresses the prior iOS crash where MP4s whose `moov` atom isn't at the front were unplayable.

**Status**: **Closed** — adapter-only re-implementation on top of the recently shipped end-to-end Range path (the Cloudflare Worker now forwards `Range` to R2 and returns `206 Partial Content`). If iOS regressions surface in the field, the next things to test are upstream `Content-Type` precision and AVPlayer's behavior across redirects.

---

### 34. Performance Gaps (not endpoint-specific)

These are architectural limitations documented in `docs/architecture/adapter-architecture.md`:

**Load-all-then-paginate pattern**: People, albums, album statistics, and asset statistics still exhaust the full result set in memory before shaping the Immich response. These paths now scan at `GUMNUT_API_MAX_PAGE_SIZE` to cut upstream round-trips, but the latency and memory profile still scale with total entity count rather than the requested page size.

**User impact**: **Medium** — Performance degrades with library size. Not visible to small libraries but will become a problem at scale.

**Dependency**: **Both** — the Gumnut API needs server-side sorting and filtering for people and albums (currently only cursor-based pagination without sort control). Adapter can then use true cursor pagination.

**Effort**: **L** — Backend API design for server-side sort/filter. Adapter refactoring for cursor-based people/album listing.

**Recommendation**: **Close** — Important for scaling. Track as a separate task.

---

## Immich v3 New Feature Areas — Scope Decisions

The v3.0 retarget added 17 new endpoints across six feature areas (enumerated in
`immich-v3-api-changes.md` §4). This section records the in-scope / stub /
intentional-gap decision for each, resolving that document's open scoping
question.

The decisions turn on **reachability** for this deployment (single-tenant, Clerk
OAuth, no Administration UI, `realtimeTranscoding: false`): whether the v3 web
*and* mobile clients actually call an endpoint on a path our users exercise.
Unimplemented paths return a FastAPI 404; the web client logs failed fetches (and
on some screens shows an error toast), so **stub** means adding a benign `200` to
silence a *reachable* error, while **intentional gap** means the 404 is
unreachable or harmless and no adapter code is written.

| Feature area | Endpoints | Reached by our clients? | Decision |
|--------------|-----------|-------------------------|----------|
| Adaptive video streaming (HLS) | 4 | No — gated by `realtimeTranscoding: false`; both clients fall back to direct playback | **Intentional gap** |
| Integrity checks (admin) | 5 | No — web Administration UI only; mobile never calls | **Intentional gap** |
| OAuth backchannel logout | 1 | No — identity-provider→server call; no client caller | **Intentional gap** |
| Plugins / Workflows | 4 | Opt-in only — the Utilities → Workflows page; never on normal navigation or mobile | **Intentional gap** |
| Calendar heatmap (`/users/me`) | 1 | Niche — manual "usage stats" accordion, desktop web only | **Stub** |
| Calendar heatmap (`/admin/users/{id}`) | 1 | No — admin per-user page only | **Intentional gap** |
| Albums — map markers | 1 | Yes — every album open (web) | **Closed** |

**Adaptive video streaming (HLS)** — `GET /assets/{id}/video/stream/*` (+ session
`DELETE`). Both the v3 web and mobile players branch on the `realtimeTranscoding`
server feature; because the adapter reports it `false` (`routers/api/server.py`),
neither ever constructs a stream URL — they use direct
`GET /assets/{id}/video/playback` (gap #33, closed). The endpoints are never
requested, so a stub would be unreachable code. A real implementation is out of
scope regardless: the Gumnut API serves stored originals with no server-side
transcoding. **Intentional gap.**

**Integrity checks (admin)** — `/admin/integrity/{report,summary,…}` (5). A
storage-layer audit (untracked files, missing files, checksum mismatch) reachable
only from the web Administration UI, which this deployment's users never enter;
mobile never calls it. There is no Gumnut API primitive for a storage audit.
**Intentional gap**, consistent with the admin/system areas #16, #17, #19, #27,
#28.

**OAuth backchannel logout** — `POST /oauth/backchannel-logout`. OIDC
back-channel logout is a server-to-server call from the identity provider to a
registered relying party; no browser, mobile, or SDK code calls it. The adapter
is not the OIDC relying party — all OAuth/JWT validation is delegated to the
Gumnut backend — so it can neither validate the logout token nor map its `sid`
claim to a session. User-initiated logout is already served by
`POST /api/auth/logout`, and the new `SystemConfigOAuthDto` fields
(`endSessionEndpoint`, `allowInsecureRequests`, `prompt`) are satisfied by benign
config defaults. **Intentional gap**, consistent with #31.

**Plugins / Workflows (experimental)** — `/plugins/methods`,
`/plugins/templates`, `/workflows/triggers`, `/workflows/{id}/share`. v3
restructured the model (`actions`/`filters` → `steps`/`methods`/`trigger`); the
verdict from #29 and #30 is unchanged. These fire only when a user opens the
Utilities → Workflows page — never on normal navigation and never on mobile — so
ordinary use produces no error. One nuance: the Workflows route loader awaits an
un-caught `Promise.all`, so a 404 there renders a SvelteKit error boundary rather
than a bare console line; that is acceptable for an opt-in, unsupported utility.
**Intentional gap.** *Optional softening (not planned):* stub the three `GET`
reads and `GET /workflows` (#29) as `200 []` to render an empty page instead of
the error boundary; leave `/workflows/{id}/share` at 404 (unreachable when the
list is empty).

**Calendar heatmap** — `GET /users/me/calendar-heatmap` (+
`GET /admin/users/{id}/calendar-heatmap`). Per-day activity counts. The user
endpoint is called only when a user manually expands the desktop-only "usage
stats" accordion in Settings (never on primary navigation, never on mobile), and
the client component has no error handler, so a 404 surfaces as a console error
and two non-rendering widgets. A real implementation is materially more expensive
than a stub — the Gumnut API's asset-count endpoint groups by month/capture-time
only, whereas a heatmap needs per-day cells and an upload-date mode (which wants
backend day-bucketing) — so it is not warranted for this widget. **Stub** the
user endpoint; **intentional gap** for the admin variant. Stub shape: `200` with
`{"from": <from param>, "to": <to param>, "totalCount": 0, "series": []}`,
echoing the requested window so the client's date parse stays valid; an empty
`series` renders a clean empty grid.

**Albums — map markers** — `GET /albums/{id}/map-markers`. New in v3; the global
`GET /map/markers` dropped its `albumIds` filter, so the v3 web album page calls
this album-scoped endpoint on every album open. Its 404 produced a console error
and a user-visible "Something went wrong" toast on a high-traffic screen (the
album still rendered). The existing `/api/map/markers` implementation (gap #3a)
is directly reusable — the Gumnut client accepts `album_id` and a world `bbox`
together — so a faithful implementation costs only a few lines more than a `[]`
stub and is strictly better (real pins vs. a permanently empty map).
**Closed** (adapter-only, effort S).

**Status:** the calendar-heatmap user-endpoint stub is implemented in
`routers/api/users.py`. Album map markers are implemented in
`routers/api/albums.py`, with the shared marker retrieval in
`routers/utils/map_markers.py`. All other areas are intentional gaps with no
adapter code.

---

## Priority Summary

### Tier 1: Close Now (high value, reasonable effort)

| Gap | Effort | Dependency | Rationale |
|-----|--------|------------|-----------|
| ~~#21 Face create~~ | — | — | Closed — `POST /api/faces` draws a user box and links it to a person; needed `gumnut-sdk >= 0.116.0` |
| ~~#12 Search random/explore~~ | — | — | Closed — random samples uniformly via month-bucket counts; explore returns cities + recents groups |
| #5 Download / archive | M | Adapter-only | Pure adapter work, clear user value |
| ~~#33 Video playback (mobile)~~ | — | — | Closed — CDN streaming with Range support; advertises `Accept-Ranges: bytes` on 200 for iOS AVPlayer |

### Tier 2: Close Next (moderate value or effort)

| Gap | Effort | Dependency | Rationale |
|-----|--------|------------|-----------|
| ~~#3a Map markers~~ | — | — | Closed — server-side geotag filter via `client.assets.list(bbox=...)`; 2000-marker cap |
| #34 Performance (pagination) | L | Both | Scaling requirement |
| #8 Memories (write path) | S | Both | Read path shipped; only save/hide persistence remains |
| #2 Tags | L | Both | Power-user organization — immich-go import path emulated via description-append; full tag entity still needs the backend |

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

> New Immich v3 feature areas (HLS streaming, integrity checks, OAuth backchannel
> logout, plugins/workflows) are additional intentional gaps — see *Immich v3 New
> Feature Areas — Scope Decisions* for the reachability analysis behind each.

## Stub Behavior Recommendation

Many stubs currently return fake success responses (HTTP 200 with dummy data), which misleads users into thinking features work when they don't. For example, creating a shared link returns a fake response with dummy IDs — the user thinks it worked, but the link is non-functional.

**Recommended approach**: Stubs for user-facing mutation endpoints (create, update, delete) should return an appropriate error rather than fake success:

- **Read endpoints** (list, get): Return empty lists or 404 for entity lookups. This is generally harmless — users see "no items" rather than a broken feature. **Caveat**: If the corresponding write stub returns fake success (e.g., creating a shared link appears to succeed), a subsequent read returning empty looks like data loss rather than an unsupported feature. For these cases, switching the write stub to 501 first eliminates the inconsistency.
- **Write endpoints** (create, update, delete): Return 501 Not Implemented with a clear message (e.g., `"Shared links are not yet supported"`). This is honest and prevents users from thinking data was saved.
- **Endpoints that disable Immich UI**: Some Immich client features check whether an endpoint returns success before showing UI elements. Returning 501 on these endpoints may cause the client to hide the feature entirely, which is the desired behavior for unimplemented features.

The `GET /server/features` fix (gap #14) is the primary mechanism for hiding unsupported UI. Switching write stubs to 501 is a complementary measure for features that the features endpoint doesn't control.

> **Design decision —** Changing stubs from fake-success to 501 is a low-risk improvement that should be done alongside the server features endpoint fix (gap #14). Together, they give users an honest picture of what the adapter supports. Read-only stubs can continue returning empty data since this is harmless. Before bulk-converting stubs to 501, test how Immich web and mobile clients display 501 errors to ensure they degrade gracefully — if clients show confusing errors or crash, a softer approach (e.g., 404 with a descriptive message) may be preferable for some endpoints.

## Version target and future considerations

The adapter targets Immich v3.0.3. References to v2.7.5 elsewhere in this document are the historical baseline used for the v2-to-v3 comparison. Future Immich releases may introduce:

- **New API endpoints** not present in v3.0.3 — these would require new stubs at minimum
- **Changed request/response schemas** — the model generator (`tools/generate_immich_models.py`) and API compatibility validator (`tools/validate_api_compatibility.py`) can detect these
- **New sync entity types** — the sync stream implementation would need new converters
- **Breaking client changes** — newer Immich clients may require endpoints or fields that the adapter doesn't provide, causing errors rather than graceful degradation

> **Design decision —** Before upgrading the target Immich version, run the API compatibility validator against the new spec to identify breaking changes. The `.immich-container-tag` file controls which web UI version is bundled, so upgrading the container tag without updating the adapter could break the web UI.

## Verification

This document's accuracy can be verified by:

1. **Running the API compatibility tool** against the full Immich v3.0.3 spec to confirm endpoint coverage:
   ```bash
   uv run tools/validate_api_compatibility.py \
     --immich-spec=https://raw.githubusercontent.com/immich-app/immich/v3.0.3/open-api/immich-openapi-specs.json \
     --adapter-spec=http://localhost:3001/openapi.json
   ```
2. **Testing each stub endpoint** to confirm it returns the documented behavior
3. **Checking the Gumnut API capabilities** to verify dependency classifications (which gaps need backend work vs. adapter-only)
