---
title: "Immich WebSocket Events Reference"
last-updated: 2026-07-12
---

# Immich WebSocket Events Reference

## Summary Table

| Event | Trigger | Payload | Web Client | Mobile Client |
|-------|---------|---------|------------|---------------|
| `on_upload_success` | Images: upload write completes; videos: deferred emit to wait for still-image variants | `AssetResponseDto` | Global listener | Legacy listener |
| `AssetUploadReadyV1` | Emitted alongside `on_upload_success` with the same image/video timing split | `SyncAssetV1` + `SyncAssetExifV1` | Not used | v2 sync protocol |
| `on_asset_delete` | Asset permanently deleted | `assetId: string` | Global listener | Listener |
| `on_asset_trash` | Asset moved to trash | `assetIds: string[]` | Global listener | Listener |
| `on_asset_restore` | Asset restored from trash | `assetIds: string[]` | Global listener | Listener |
| `on_asset_update` | Sidecar metadata extracted (upstream) / asset metadata edited (adapter) | `AssetResponseDto` | Global listener | Listener |
| `on_asset_stack_update` | Stack created/updated/deleted | None | Global listener | Listener |
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
**Payload**: Compact sync format — `SyncAssetV1` asset + `SyncAssetExifV1` exif (see `routers/immich_models.py`)

**Client handling**:
- **Web**: Not used
- **Mobile**: v2 sync protocol. Batches events and updates local SQLite database for real-time multi-device sync.

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
- **immich-adapter**: Emitted after a successful single-asset metadata edit via `PUT /api/assets/{id}` (description / paired latitude+longitude / dateTimeOriginal). The adapter has no sidecar processing, so this is the only emission path here.

**Sent to**: Asset owner (by userId)
**Payload**: Full `AssetResponseDto`

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener, triggers sync

**Note**: Sidecar files (XMP) store metadata alongside photos and sync bidirectionally with the database.

---

### `on_asset_stack_update`

**Sent to**: Stack owner (by userId)

Otherwise as in the Summary Table; emitted from `notification.service.ts`, and the mobile listener triggers a sync.

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
