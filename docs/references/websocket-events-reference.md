---
title: "Immich WebSocket Events Reference"
last-updated: 2026-08-25
---

# Immich WebSocket Events Reference

## Summary Table

Client columns are read from the pinned upstream source for **both** clients (`web/src/lib/` and `mobile/lib/` at the tag in `.immich-container-tag`); "Not used" means neither registers a handler, not that web alone doesn't.

| Event | Trigger | Payload | Web Client | Mobile Client |
|-------|---------|---------|------------|---------------|
| `on_upload_success` | Images: upload write completes; videos: deferred emit to wait for still-image variants | `AssetResponseDto` | Global listener | Legacy listener |
| `AssetUploadReadyV1` | Emitted alongside `on_upload_success` with the same image/video timing split | `SyncAssetV1` + `SyncAssetExifV1` | Not used | v2 sync protocol |
| `AssetEditReadyV2` | Edit-route write commits (PUT and DELETE on `/api/assets/{id}/edits`) | `SyncAssetV2` + `SyncAssetEditV1[]` | Editor apply/remove wait | Upserts asset + edit rows |
| `on_asset_delete` | Asset permanently deleted | `assetId: string` | Global listener | Listener |
| `on_asset_trash` | Asset moved to trash | `assetIds: string[]` | Global listener | Listener |
| `on_asset_restore` | Asset restored from trash | `assetIds: string[]` | Global listener | Listener |
| `on_asset_update` | Sidecar metadata extracted (upstream) / asset metadata edited or edit-route write committed (adapter) | `AssetResponseDto` | Global listener | Listener |
| `on_asset_stack_update` | Stack created/updated/deleted | None | Declared, not subscribed | Not referenced |
| `on_asset_hidden` | Asset visibility changed | `assetId: string` | Global listener | Listener |
| `on_person_thumbnail` | Person thumbnail generated | `personId: string` | Page-specific | Not used |
| `on_session_delete` | Session invalidated | `sessionId: string` | Global (triggers logout) | Not used |
| `on_notification` | In-app notification created | `NotificationDto` | Global (refreshes panel) | Not used |
| `on_config_update` | System config changed | None | Global listener | Listener |
| `on_new_release` | New version available | `ReleaseNotification` | Global listener | Listener |
| `on_server_version` | Connection established | `ServerVersionResponseDto` | Global listener | Not documented |
| `on_user_delete` | User account deleted | `userId: string` | Global listener | Not documented |

---

## Event Details

### `on_upload_success`

**Upstream trigger**: Emitted when the `AssetGenerateThumbnails` job completes (`job.service.ts`).

**Adapter trigger**:
- **Images**: emitted synchronously from the upload handler. Image variants (`thumbnail`, `preview`, `fullsize`) are CDN-resized URLs to the same uploaded file, so they're available the moment the upload write completes.
- **Videos**: emission is **deferred** by `_VIDEO_EMIT_DELAY_SECONDS` (defined in `routers/api/assets.py`) via a detached `asyncio.create_task`. Video still-image variants (`thumbnail_image`, `preview_image`, `fullsize_image`) live at a separate `derived_path` that only exists after the Gumnut API's ffmpeg extraction finishes — without the delay, the Immich web client receives `on_upload_success`, inserts the asset into the timeline grid, then renders "Error loading image" because the thumbnail URL still 404s. The HTTP `POST /api/assets` 201 response is **not** delayed; only the WebSocket emission waits.

**Sent to**: Asset owner (by userId)
**Payload**: Full `AssetResponseDto` (see `routers/immich_models.py`)

**Client handling**:
- **Web**: Global listener via `websocketEvents`
- **Mobile**: Legacy listener; being replaced by `AssetUploadReadyV1`

**Note**: Immich has a TODO to deprecate this in favor of `AssetUploadReadyV1`.

---

### `AssetUploadReadyV1`

**Upstream trigger**: Emitted alongside `on_upload_success` when thumbnail generation completes (`job.service.ts`).

**Adapter trigger**:
- Emitted from the same helper as `on_upload_success`, so the timing stays aligned across both upload-success events.
- **Images**: emitted synchronously from the upload handler.
- **Videos**: emitted after the same `_VIDEO_EMIT_DELAY_SECONDS` deferral used for `on_upload_success`, so mobile clients do not hear about a new upload before the video's still-image variants usually exist.

**Sent to**: Asset owner (by userId)
**Payload**: Compact sync format — `SyncAssetV1` asset + `SyncAssetExifV1` exif (see `routers/immich_models.py`). The asset's `stackId` reflects burst-stack membership (mapped via `immich_stack_id`) and is `null` for a loose asset — usually the case at upload time, since bursts are detected afterward.

**Client handling**:
- **Web**: Not used
- **Mobile**: v2 sync protocol. Batches events and updates local SQLite database for real-time multi-device sync.

---

### `AssetEditReadyV2`

**Upstream trigger**: Emitted when the `AssetEditThumbnailGeneration` job finishes re-rendering an edited asset (`job.service.ts`), for both edit apply and edit removal.

**Adapter trigger**: Emitted from `_emit_edit_committed_events` in `routers/api/assets.py` after an edits-route write commits — a successful `PUT /api/assets/{id}/edits` (append or in-place replace) or `DELETE /api/assets/{id}/edits` (including the idempotent root-current delete, which changes nothing but must still emit). Never emitted on a failed or CAS-losing write.

- **Superseded before the refresh**: when the post-commit retrieve shows another write already replaced this one (`current_version_id` differs), the outcome depends on the winner's kind. A newer `edit`/`edit:*` current version came through these routes — the event's only emitter — and emits its own consistent event, which still resolves this client's wait (it filters on `asset.id` alone), so this request's stale event is suppressed. Any other winner (`original`, `external:*`) never emits, so this request emits the refreshed asset row with an empty edit list instead, matching how GET reads root and opaque tips.
- **Readiness wait**: the adapter bakes synchronously inside the request, but the event means "the edited renders are ready to fetch": on receipt the clients re-read the asset and key image URLs on `thumbhash` (the `c=` cache param), which the Gumnut API recomputes from the new current version in a background task. So before emitting, the adapter polls the asset until `thumbhash` rotates away from its pre-commit value (`_retrieve_asset_for_edit_event`, deadline bounded well inside the 10-second client wait); at the deadline it emits anyway — the commit is durable and the next asset read heals the display. Writes that store nothing skip the wait: the idempotent root-current delete, and a PUT whose rendered bytes and params exactly duplicate the current version (an unchanged re-save; the Gumnut API answers `200` with the existing version instead of `201`).
- **Ordering**: the emission is awaited just before returning, so the event precedes the HTTP response.
- **Failure semantics**: everything after the durable write is best-effort — transport failures are swallowed, and if the post-commit asset read or payload conversion fails, the route logs, returns success, and emits neither event. The client's wait then times out, and its retry re-applies the same wholesale recipe idempotently, whereas a 5xx would misreport a committed write as failed.

**Sent to**: Asset owner (by userId)
**Payload**: `{ asset: SyncAssetV2, edit: SyncAssetEditV1[] }` — the refreshed sync asset row plus the complete current edit-action list (empty after a delete). See `AssetEditReadyV2Payload` in `services/websockets.py`.

**Client handling**:
- **Web**: The editor's apply flow registers a one-shot 10-second wait for this event, filtered on `payload.asset.id`, *before* sending the PUT/DELETE, and treats a timeout as a failed apply — every successful edit write must emit exactly one.
- **Mobile**: Its editor's `applyEdits` arms the same 10-second `asset.id`-filtered wait before the PUT/DELETE. On receipt the event is dispatched to `syncWebsocketEditV2`, which upserts `payload.asset` through the v2 asset-sync path and replaces the asset's local edit rows with `payload.edit` (an event arriving while one is still processing is dropped, not queued). A malformed payload (non-object, missing `asset`) is logged and dropped, so the adapter must always send both fields.

**Companion event**: the same commit also emits `on_asset_update` with the full refreshed `AssetResponseDto`, which the editor panel and timeline use to refresh `isEdited`, dimensions, MIME type, and URLs without a reload.

---

### `on_asset_delete`

**Sent to**: Asset owner (by userId)

Otherwise as in the Summary Table; emitted from `notification.service.ts`, and the mobile listener triggers a sync.

---

### `on_asset_trash`

**Sent to**: Asset owner (by userId)

Otherwise as in the Summary Table; emitted from `notification.service.ts`, and the mobile listener triggers a sync.

---

### `on_asset_restore`

**Sent to**: Asset owner (by userId)

Otherwise as in the Summary Table; emitted from `notification.service.ts`, and the mobile listener triggers a sync.

---

### `on_asset_update`

**Trigger**:
- **Upstream Immich**: Emitted when metadata extracted from sidecar files (`notification.service.ts`). Only triggered by sidecar processing, NOT by direct user edits.
- **immich-adapter**: Emitted after a successful single-asset metadata edit via `PUT /api/assets/{id}` (description / paired latitude+longitude / dateTimeOriginal), and as the companion of `AssetEditReadyV2` after every committed edit-route write (`PUT`/`DELETE /api/assets/{id}/edits`, including the idempotent root-current delete — see that event). The adapter has no sidecar processing.

**Sent to**: Asset owner (by userId)
**Payload**: Full `AssetResponseDto`

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener, triggers sync

**Note**: Sidecar files (XMP) store metadata alongside photos and sync bidirectionally with the database.

---

### `on_asset_stack_update`

**Sent to**: Stack owner (by userId)
**Payload**: none — the event carries no data (see below)

**Trigger**:
- **Upstream Immich**: emitted from `notification.service.ts` on `StackCreate`, `StackUpdate`, `StackDelete`, and `StackDeleteAll`.
- **immich-adapter**: emitted from the five mutating stack routes in `routers/api/stacks.py`, one emit per successful mutation — `create_stack`, `update_stack` (only when the cover actually changes; a cover-less PUT is a pure read and emits nothing), `remove_asset_from_stack`, `delete_stack`, and `delete_stacks` (a single emit after a clean bulk dissolve). All are scoped to the owning user's room.

**Payload shape**: none. Upstream calls `clientSend('on_asset_stack_update', userId)` with no data argument — the repository type `on_asset_stack_update: string[]` is a rest-param (`...data: string[]`) that spreads to zero args, not an id array. The adapter therefore emits with `payload=None` via `emit_user_event`, never `emit_user_event_per_id`.

**Client handling**:
- **Web**: Declared in the `Events` type map but **not subscribed** — no `.on('on_asset_stack_update', …)` handler is registered.
- **Mobile**: Not referenced.

**Note**: Because no current Immich client subscribes, this emission is upstream-parity / forward-compat rather than an immediate UI trigger. The durable convergence path for stack changes is the event-driven sync stream, not this hint.

---

### `on_asset_hidden`

**Trigger**: Emitted when asset visibility changes to hidden (`notification.service.ts`). Used for hiding live photo motion video components.
**Sent to**: Asset owner (by userId)
**Payload**: `assetId: string`

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener

---

### `on_person_thumbnail`

**Trigger**: Emitted when the `PersonGenerateThumbnail` job completes (`job.service.ts`).
**Sent to**: Person owner (by userId)
**Payload**: `personId: string`

**Purpose**: Cache busting. The thumbnail URL includes `updatedAt` as a query parameter:

```typescript
`/api/people/${personId}/thumbnail?updatedAt=${person.updatedAt}`
```

When received, the client updates `person.updatedAt` to force the browser to fetch the fresh thumbnail.

**Client handling**:
- **Web**: Page-specific listener (not global). Only active on `/explore`, `/people`, `/people/[personId]`. When on other pages, the event is received but ignored.
- **Mobile**: Does NOT listen to this event.

---

### `on_session_delete`

**Trigger**: Emitted when session is invalidated (`notification.service.ts`).
**Sent to**: Session room (by sessionId)
**Payload**: `sessionId: string`

**Scenarios**:
- User logout via `/api/auth/logout`
- User deletes session via `/api/sessions/{id}`
- Password change with `invalidateSessions: true` (does NOT emit individual events)

**Client handling**:
- **Web**: Global listener. Triggers `authManager.logout()`.
- **Mobile**: Not used.

**Note**: Event is sent with a 500ms delay after the response.

---

### `on_notification`

**Trigger**: Emitted when in-app notification is created (`notification.service.ts`).
**Sent to**: Notification recipient (by userId)
**Payload**: `NotificationDto` (see `routers/immich_models.py`)

**Notification triggers**:

| Trigger | Recipient | Type | Level | Description |
|---------|-----------|------|-------|-------------|
| Database backup job fails | Admin | `JobFailed` | Error | "Job {name} failed with error: {message}" |
| User invited to shared album | Invited user | `AlbumInvite` | Success | "{sender} shared an album ({name}) with you" |
| New media added to shared album | Album members | `AlbumUpdate` | Info | "New media has been added to the album ({name})" |

**Client handling**:
- **Web**: Global listener. Calls `notificationManager.refresh()` to fetch updated notifications. Displays in bell icon dropdown panel with colored icons, title, description, relative timestamp, and unread indicator. Album notifications navigate to `/albums/{albumId}` on click.
- **Mobile**: Does NOT listen to this event.

---

### `on_config_update`

**Sent to**: All connected clients (broadcast)

Otherwise as in the Summary Table; emitted from `notification.service.ts`.

---

### `on_new_release`

**Trigger**: Emitted when background job detects new GitHub release (`version.service.ts`).
**Sent to**: All connected clients (broadcast)
**Payload**: `ReleaseNotification` (see `routers/immich_models.py`)

**Client handling**:
- **Web**: Global listener. Updates `websocketStore.release`.
- **Mobile**: Listener

---

### `on_server_version`

**Sent to**: Connecting client

Otherwise as in the Summary Table; sent on WebSocket connection establishment, and the web listener updates `websocketStore.serverVersion`.

---

### `on_user_delete`

**Sent to**: All connected clients (broadcast)

Otherwise as in the Summary Table; emitted from `notification.service.ts`.

---

## Client Event Registration

Per-event subscriptions are listed in the Summary Table's Web/Mobile columns and, for events with a detailed section, that event's "Client handling" subsection. The grouping below covers only the registration distinctions not visible there.

### Web Client (`websocket.ts`)

A few events have **global listeners** that are always active when connected (`on_server_version`, `on_new_release`, `on_session_delete`, `on_notification`). The rest are **page-specific listeners** subscribed via `websocketEvents.on()` — notably `on_person_thumbnail`, which is only active on people-related pages.

### Mobile Client (`websocket.provider.dart`)

The mobile client uses `AssetUploadReadyV1` in v2 sync mode and the `on_asset_*` events in legacy mode (see the Summary Table). It does not listen to `on_person_thumbnail`, `on_session_delete`, or `on_notification`.
