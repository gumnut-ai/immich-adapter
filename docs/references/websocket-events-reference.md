---
title: "Immich WebSocket Events Reference"
last-updated: 2026-01-09
---

# Immich WebSocket Events Reference

This document describes the WebSocket events emitted by the Immich server and how clients handle them.

## Summary Table

| Event | Trigger | Payload | Web Client | Mobile Client |
|-------|---------|---------|------------|---------------|
| `on_upload_success` | Thumbnail generation completes | `AssetResponseDto` | Global listener | Legacy listener |
| `AssetUploadReadyV1` | Thumbnail generation completes | `SyncAssetV1` + `SyncAssetExifV1` | Not used | v2 sync protocol |
| `on_asset_delete` | Asset permanently deleted | `assetId: string` | Global listener | Listener |
| `on_asset_trash` | Asset moved to trash | `assetIds: string[]` | Global listener | Listener |
| `on_asset_restore` | Asset restored from trash | `assetIds: string[]` | Global listener | Listener |
| `on_asset_update` | Sidecar metadata extracted | `AssetResponseDto` | Global listener | Listener |
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

**Trigger**: Emitted when `AssetGenerateThumbnails` job completes (`job.service.ts:343-366`).
**Sent to**: Asset owner (by userId)
**Payload**: Full `AssetResponseDto`

```typescript
AssetResponseDto {
  id: string;
  ownerId: string;
  originalFileName: string;
  thumbhash: string | null;
  fileCreatedAt: string;
  fileModifiedAt: string;
  localDateTime: string;
  type: AssetType;
  // ... full asset details
}
```

**Client handling**:
- **Web**: Global listener via `websocketEvents`
- **Mobile**: Legacy listener; being replaced by `AssetUploadReadyV1`

**Note**: Immich has a TODO to deprecate this in favor of `AssetUploadReadyV1`.

---

### `AssetUploadReadyV1`

**Trigger**: Emitted alongside `on_upload_success` when thumbnail generation completes (`job.service.ts:369`).
**Sent to**: Asset owner (by userId)
**Payload**: Compact sync format

```typescript
{
  asset: SyncAssetV1 {
    id: string;
    ownerId: string;
    originalFileName: string;
    thumbhash: string | null;
    checksum: string;
    fileCreatedAt: string;
    fileModifiedAt: string;
    localDateTime: string;
    duration: string;
    type: AssetType;
    deletedAt: string | null;
    isFavorite: boolean;
    visibility: AssetVisibility;
    livePhotoVideoId: string | null;
    stackId: string | null;
    libraryId: string | null;
  };
  exif: SyncAssetExifV1 {
    assetId: string;
    description: string | null;
    exifImageWidth: number | null;
    exifImageHeight: number | null;
    fileSizeInByte: number | null;
    orientation: string | null;
    dateTimeOriginal: string | null;
    modifyDate: string | null;
    timeZone: string | null;
    latitude: number | null;
    longitude: number | null;
    // ... additional EXIF fields
  };
}
```

**Client handling**:
- **Web**: Not used
- **Mobile**: v2 sync protocol. Batches events and updates local SQLite database for real-time multi-device sync.

---

### `on_asset_delete`

**Trigger**: Emitted when asset is permanently deleted (`notification.service.ts:147-150`).
**Sent to**: Asset owner (by userId)
**Payload**: `assetId: string`

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener, triggers sync

---

### `on_asset_trash`

**Trigger**: Emitted when asset(s) moved to trash (`notification.service.ts:142-145`, `152-155`).
**Sent to**: Asset owner (by userId)
**Payload**: `assetIds: string[]`

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener, triggers sync

---

### `on_asset_restore`

**Trigger**: Emitted when asset(s) restored from trash (`notification.service.ts:173-176`).
**Sent to**: Asset owner (by userId)
**Payload**: `assetIds: string[]`

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener, triggers sync

---

### `on_asset_update`

**Trigger**: Emitted when metadata extracted from sidecar files (`notification.service.ts:157-171`). Only triggered by sidecar processing, NOT by direct user edits.
**Sent to**: Asset owner (by userId)
**Payload**: Full `AssetResponseDto`

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener, triggers sync

**Note**: Sidecar files (XMP) store metadata alongside photos and sync bidirectionally with the database.

---

### `on_asset_stack_update`

**Trigger**: Emitted when stack is created, updated, or deleted (`notification.service.ts:178-196`).
**Sent to**: Stack owner (by userId)
**Payload**: None (empty)

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener, triggers sync

---

### `on_asset_hidden`

**Trigger**: Emitted when asset visibility changes to hidden (`notification.service.ts:132-135`). Used for hiding live photo motion video components.
**Sent to**: Asset owner (by userId)
**Payload**: `assetId: string`

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener

---

### `on_person_thumbnail`

**Trigger**: Emitted when `PersonGenerateThumbnail` job completes (`job.service.ts:334-340`).
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

**Trigger**: Emitted when session is invalidated (`notification.service.ts:224-228`).
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

**Trigger**: Emitted when in-app notification is created (`notification.service.ts:101`, `467`).
**Sent to**: Notification recipient (by userId)
**Payload**:

```typescript
NotificationDto {
  id: string;
  createdAt: Date;
  level: 'success' | 'error' | 'warning' | 'info';
  type: 'JobFailed' | 'BackupFailed' | 'SystemMessage' | 'AlbumInvite' | 'AlbumUpdate' | 'Custom';
  title: string;
  description?: string;
  data?: any;       // e.g., { albumId: "uuid" } for album notifications
  readAt?: Date;
}
```

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

**Trigger**: Emitted when admin updates system configuration (`notification.service.ts:111-115`).
**Sent to**: All connected clients (broadcast)
**Payload**: None (empty)

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener

---

### `on_new_release`

**Trigger**: Emitted when background job detects new GitHub release (`version.service.ts:62-103`).
**Sent to**: All connected clients (broadcast)
**Payload**:

```typescript
ReleaseNotification {
  isAvailable: boolean;
  checkedAt: string;  // ISO8601
  serverVersion: ServerVersionResponseDto;
  releaseVersion: ServerVersionResponseDto;
}
```

**Client handling**:
- **Web**: Global listener. Updates `websocketStore.release`.
- **Mobile**: Listener

---

### `on_server_version`

**Trigger**: Sent on WebSocket connection establishment.
**Sent to**: Connecting client
**Payload**: `ServerVersionResponseDto`

**Client handling**:
- **Web**: Global listener. Updates `websocketStore.serverVersion`.

---

### `on_user_delete`

**Trigger**: Emitted when user account is deleted (`notification.service.ts:205-208`).
**Sent to**: All connected clients (broadcast)
**Payload**: `userId: string`

**Client handling**:
- **Web**: Global listener

---

## Client Event Registration

### Web Client (`websocket.ts`)

**Global listeners** (always active when connected):
- `on_server_version`
- `on_new_release`
- `on_session_delete`
- `on_notification`

**Page-specific listeners** (via `websocketEvents.on()`):
- `on_person_thumbnail` - only on people-related pages
- Other asset events - subscribed by pages as needed

### Mobile Client (`websocket.provider.dart`)

**v2 sync mode**:
- `AssetUploadReadyV1`

**Legacy mode**:
- `on_upload_success`
- `on_asset_delete`
- `on_asset_trash`
- `on_asset_restore`
- `on_asset_update`
- `on_asset_stack_update`
- `on_asset_hidden`

**Always active**:
- `on_config_update`
- `on_new_release`

**Not used**:
- `on_person_thumbnail`
- `on_session_delete`
- `on_notification`
