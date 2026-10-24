---
title: "Immich WebSocket Events Reference"
last-updated: 2026-10-23
---

# Immich WebSocket Events Reference

This reference covers upstream Immich event contracts and adapter behavior;
events the adapter does not emit are marked upstream-only.

## Summary Table

Client columns report registered handlers in both pinned upstream clients
(`web/src/` and `mobile/lib/` at the tag in `.immich-container-tag`).

| Event | Trigger | Payload | Web Client | Mobile Client |
|-------|---------|---------|------------|---------------|
| `on_upload_success` | Images: upload write completes; videos: deferred emit to wait for still-image variants | `AssetResponseDto` | Timeline/viewer listeners | Not used |
| `AssetUploadReadyV1` | Emitted alongside `on_upload_success` with the same image/video timing split | `SyncAssetV1` + `SyncAssetExifV1` | Not used | Batched sync |
| `AssetEditReadyV2` | Edit-route write commits (PUT and DELETE on `/api/assets/{id}/edits`) | `SyncAssetV2` + `SyncAssetEditV1[]` | Editor apply/remove wait | Upserts asset + edit rows |
| `on_asset_delete` | Asset permanently deleted | `assetId: string` | Global listener | Listener |
| `on_asset_trash` | Asset moved to trash | `assetIds: string[]` | Global listener | Listener |
| `on_asset_restore` | Asset restored from trash | `assetIds: string[]` | Declared, not subscribed | Listener |
| `on_asset_update` | Sidecar metadata extracted (upstream) / description, location, datetime, favorite, or rating edited, or edit-route write committed (adapter) | `AssetResponseDto` | Global listener | Listener |
| `on_asset_stack_update` | Stack created/updated/deleted | None | Declared, not subscribed | Triggers sync |
| `on_asset_hidden` | Upstream only: asset visibility changed | `assetId: string` | Declared, not subscribed | Listener |
| `on_person_thumbnail` | Upstream only: person thumbnail generated | `personId: string` | Global bridge + component listeners | Not used |
| `on_session_delete` | Session invalidated (upstream) / session deleted (adapter) | `sessionId: string` | Global (triggers logout) | Not used |
| `on_notification` | Upstream only: in-app notification created | `NotificationDto` | Global (refreshes panel) | Not used |
| `on_config_update` | Upstream only: system config changed | None | Declared, not subscribed | Listener |
| `on_new_release` | Upstream only: new version available | `ReleaseEventV1` | Global listener | Listener |
| `on_server_version` | Connection established | `ServerVersionResponseDto` | Global listener | Not used |
| `on_user_delete` | Upstream only: user account deleted | `userId: string` | Global listener | Not used |

---

## Event Details

### `on_upload_success`

**Upstream trigger**: Emitted when the `AssetGenerateThumbnails` job completes (`job.service.ts`).

**Adapter trigger**: Successful uploads, owned by `upload_asset` in
`routers/api/assets.py`.
- **Images**: emitted before the HTTP response; CDN-resized image variants
  are available when the upload commits.
- **Videos**: deferred to allow the Gumnut API to extract still-image variants
  before timeline insertion. This delay reduces thumbnail 404s; it does not
  verify readiness or delay the HTTP `POST /api/assets` 201 response.

**Sent to**: Asset owner (by userId)
**Payload**: Full `AssetResponseDto` (see `routers/immich_models.py`)

**Client handling**:
- **Web**: Timeline and asset-viewer listeners via `websocketEvents`.
- **Mobile**: Not used; subscribes to versioned upload-ready events instead.

**Note**: Pinned upstream plans to remove this in favor of its versioned
upload-ready event (`AssetUploadReadyV2` in `job.service.ts`).

---

### `AssetUploadReadyV1`

**Upstream status**: Pinned `job.service.ts` emits `AssetUploadReadyV2` alongside
`on_upload_success` when thumbnail generation completes. The adapter emits V1;
pinned mobile still accepts and batches it in `WebsocketNotifier`.

**Adapter trigger**: Paired with `on_upload_success`, including its synchronous
image timing and deferred video timing.

**Sent to**: Asset owner (by userId)
**Payload**: Compact sync format — `SyncAssetV1` asset + `SyncAssetExifV1` exif (see `routers/immich_models.py`). The asset's `stackId` reflects burst-stack membership (mapped via `immich_stack_id`) and is `null` for a loose asset — usually the case at upload time, since bursts are detected afterward.

**Client handling**:
- **Web**: Not used
- **Mobile**: Batches events and updates local SQLite for real-time sync.

---

### `AssetEditReadyV2`

**Upstream trigger**: Emitted when the `AssetEditThumbnailGeneration` job finishes re-rendering an edited asset (`job.service.ts`), for both edit apply and edit removal.

**Adapter trigger**: Successful `PUT`/`DELETE /api/assets/{id}/edits`, including
unchanged re-saves and idempotent root-current deletes. Failed or CAS-losing
writes never emit. The route owners are `apply_asset_edits` and
`remove_asset_edits` in `routers/api/assets.py`.

**Sent to**: Asset owner (by userId)
**Payload**: `{ asset: SyncAssetV2, edit: SyncAssetEditV1[] }` — the refreshed sync asset row plus the complete current edit-action list (empty after a delete). See `AssetEditReadyV2Payload` in `services/websockets.py`.

**Readiness and durability**:
- Wait for the refreshed `thumbhash`, the clients' image cache key, before
  notifying. The Gumnut API refreshes it asynchronously after the rendered
  bytes commit. The readiness deadline is measured from **route entry**:
  rendering and upload share the web editor's already-running 10-second
  budget. At the deadline, emit the available state rather than extending the
  readiness wait; the write is durable and a later asset read heals a stale
  cache key. Writes storing nothing need no readiness wait.
- Never pair a stale recipe with a newer asset. A superseding `edit`/`edit:*`
  write emits its own consistent event, satisfying the web wait's asset-ID
  filter, so suppress the older notification. A superseding root or opaque
  (`external:*`) writer does not emit this event; publish its refreshed state
  with an empty edit list instead.
- Notification is awaited before the HTTP response, but post-commit refresh,
  payload conversion, and transport failures remain best-effort: log and keep
  the committed write successful, even if an event is lost. A notification
  timeout must not be mistaken for a rolled-back write. Normal sync converges
  asset metadata, but its `AssetEditsV1` pass is a no-op: it cannot recover a
  missed edit-recipe payload.

**Client handling**:
- **Web**: `EditManager.applyEdits` registers an asset-ID-filtered, one-shot
  10-second wait **before** the PUT/DELETE and reports timeout as a failed apply.
- **Mobile**: `applyEdits` waits for either edit-ready version **after** the HTTP
  apply completes, swallowing timeout errors. Independently, its socket handler
  upserts `asset` through v2 sync and replaces local edit rows with `edit`.
  Malformed payloads are logged and dropped; both fields are required. Events
  arriving while WebSocket sync is busy are dropped rather than queued.

**Companion event**: the same commit also emits `on_asset_update` with the full refreshed `AssetResponseDto`, which the editor panel and timeline use to refresh `isEdited`, dimensions, MIME type, and URLs without a reload.

Pinned client owners: [web editor](https://github.com/immich-app/immich/blob/v3.2.0/web/src/lib/managers/edit/edit-manager.svelte.ts),
[mobile editor](https://github.com/immich-app/immich/blob/v3.2.0/mobile/lib/presentation/actions/edit_asset.action.dart),
and [mobile sync](https://github.com/immich-app/immich/blob/v3.2.0/mobile/lib/domain/services/sync_stream.service.dart).

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
- **immich-adapter**: Emitted after a successful single-asset metadata edit via `PUT /api/assets/{id}` (description / paired latitude+longitude / dateTimeOriginal / favorite or rating), and as the companion of `AssetEditReadyV2` after every committed edit-route write (`PUT`/`DELETE /api/assets/{id}/edits`, including the idempotent root-current delete — see that event). The adapter has no sidecar processing.

**Sent to**: Asset owner (by userId)
**Payload**: Full `AssetResponseDto`

**Client handling**:
- **Web**: Global listener
- **Mobile**: Listener, triggers sync

---

### `on_asset_stack_update`

**Sent to**: Stack owner (by userId)
**Payload**: none. Do not send an ID array; the upstream repository's
`string[]` type is a rest parameter permitting zero arguments. The adapter's
wire contract is owned by `services/websockets.py::WebSocketEvent.STACK_UPDATE`.

**Trigger**:
- **Upstream Immich**: emitted from `notification.service.ts` on `StackCreate`, `StackUpdate`, `StackDelete`, and `StackDeleteAll`.
- **immich-adapter**: successful stack mutations in `routers/api/stacks.py`
  notify once per mutation, including once for a clean bulk dissolve. Read-only
  requests and partial bulk failures do not emit.

**Client handling**:
- **Web**: Declared in the `Events` type map but **not subscribed** — no `.on('on_asset_stack_update', …)` handler is registered.
- **Mobile**: `WebsocketNotifier` subscribes and enqueues remote sync in
  [the pinned socket provider](https://github.com/immich-app/immich/blob/v3.2.0/mobile/lib/providers/websocket.provider.dart).

The event is a best-effort sync hint; the sync stream owns durable convergence.

---

### `on_asset_hidden`

**Trigger**: Emitted when asset visibility changes to hidden (`notification.service.ts`). Used for hiding live photo motion video components.
**Adapter status**: Upstream-only; this adapter does not emit this event.
**Sent to**: Asset owner (by userId)
**Payload**: `assetId: string`

**Client handling**:
- **Web**: Declared, not subscribed.
- **Mobile**: Listener

---

### `on_person_thumbnail`

**Trigger**: Emitted when the `PersonGenerateThumbnail` job completes (`job.service.ts`).
**Adapter status**: The adapter declares this event as requiring the Gumnut API
event channel, but has no current emission call site.
**Sent to**: Person owner (by userId)
**Payload**: `personId: string`

**Purpose**: Cache busting. The thumbnail URL includes `updatedAt` as a query parameter:

```typescript
`/api/people/${personId}/thumbnail?updatedAt=${person.updatedAt}`
```

People views refresh their thumbnail cache key on receipt.

**Client handling**:
- **Web**: `websocket.ts` globally bridges to `PersonThumbnailReady` for
  component consumers, including explore and the face side panel. People pages
  also subscribe directly to refresh thumbnail cache keys.
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
- **Web**: Global listener. Triggers logout navigation through the auth manager.
- **Mobile**: Not used.

**Adapter timing**: Logout and session-deletion requests await
`emit_session_event` before returning the HTTP response.

---

### `on_notification`

**Trigger**: Emitted when in-app notification is created (`notification.service.ts`).
**Adapter status**: Upstream-only; this adapter does not emit this event.
**Sent to**: Notification recipient (by userId)
**Payload**: `NotificationDto` (see `routers/immich_models.py`)

**Notification triggers**:

| Trigger | Recipient | Type | Level | Description |
|---------|-----------|------|-------|-------------|
| Database backup job fails | Admin | `JobFailed` | Error | "Job {name} failed with error: {message}" |
| User invited to shared album | Invited user | `AlbumInvite` | Success | "{sender} shared an album ({name}) with you" |
| New media added to shared album | Album members | `AlbumUpdate` | Info | "New media has been added to the album ({name})" |

**Client handling**:
- **Web**: Global listener. Calls `notificationManager.refresh()` to fetch updated notifications. Album notifications navigate to `/albums/{albumId}` on click.
- **Mobile**: Does NOT listen to this event.

---

### `on_config_update`

**Adapter status**: Upstream-only; this adapter does not emit this event.
**Sent to**: All connected clients (broadcast)

Otherwise as in the Summary Table; emitted from `notification.service.ts`.

---

### `on_new_release`

**Trigger**: Emitted when background job detects new GitHub release (`version.service.ts`).
**Adapter status**: Upstream-only; this adapter does not emit this event.
**Sent to**: All connected clients (broadcast)
**Payload**: `ReleaseEventV1` (see `routers/immich_models.py`)

**Client handling**:
- **Web**: Global listener. Dispatches `ReleaseEvent` to the release manager.
- **Mobile**: Listener

---

### `on_server_version`

**Sent to**: Connecting client

Otherwise as in the Summary Table; sent on WebSocket connection establishment, and the web listener updates `websocketStore.serverVersion`.

---

### `on_user_delete`

**Adapter status**: Upstream-only; this adapter does not emit this event.
**Sent to**: All connected clients (broadcast)

Otherwise as in the Summary Table; emitted from `notification.service.ts`.

---

## Client Event Registration

Per-event subscriptions are listed in the Summary Table's Web/Mobile columns and, for events with a detailed section, that event's "Client handling" subsection. The grouping below covers only the registration distinctions not visible there.

### Web Client (`websocket.ts`)

`websocket.ts` owns global socket listeners, often bridging into the app event
manager. Components and pages can also subscribe directly via
`websocketEvents.on()`. A declaration in the `Events` type map alone does not
register a listener.

### Mobile Client (`websocket.provider.dart`)

The mobile client registers both upload-ready versions, `AssetEditReadyV2`,
and the `on_asset_*` sync hints without a legacy-mode switch. It does not
listen to `on_upload_success`, `on_person_thumbnail`, `on_session_delete`,
or `on_notification`.
