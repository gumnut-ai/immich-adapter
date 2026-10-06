---
title: "Asset Edits and Downloads"
last-updated: 2026-10-01
---

# Asset Edits and Downloads

## Edited state and exact-original downloads

The top-level `kind` identifies the current rendering. Map `kind != "original"`
to Immich `isEdited` through `is_asset_edited`; the namespace is open, so every
non-original kind is edited. Because `kind` is a lean-core field, this requires
no additional query.

`asset_urls["original"]` points to the current rendering. For
`GET /api/assets/{id}/original`, `edited=true` streams that rendering, while the
default `edited=false` streams the version-chain root (`position == 0`) via
`select_root` in `routers/utils/asset_version_chain.py`. Keep it on the root:
Immich shows *Download original* for every asset it reports as edited, and
backup tools expect the upload, so this path must never substitute a derived
rendering.

Size follows the same split. Lean-core top-level `file_size_bytes` is the
current rendering's size, so size a stream of `asset_urls["original"]` from it.
`file_data.file_size_bytes` is the upload's size; the two differ once an asset
is edited. EXIF `fileSizeInByte` describes the upload, so it reads `file_data`.

The batch routes in `routers/api/download.py` honor `edited` the same way.
`POST /api/download/archive` defaults `edited` to `False` (matching upstream's
`dto.edited ?? false`): false/omitted streams each member's `select_root`
upload, `edited=true` streams the current rendering. `POST /api/download/info`
has no `edited` field, so its reported sizes are always position-0 original
(so `/info` and `/archive` agree for `edited=false`). Because the top-level
`kind` is lean-core, a version-chain fetch is only needed for edited members
(`kind != "original"`); root-only members resolve straight from the
`assets.list` payload. An invalid chain fails closed with the same 502 as the
single-asset route; a member with no downloadable bytes keeps the batch route's
existing 400.

The **edit base** is a different selection: the highest-position version whose
`kind` is not `edit` (or `edit:*`), from `select_edit_base` in the same module.
`services/asset_edit_renderer.py` renders every recipe from it. Skipping `edit`
versions keeps repeated adjustments non-cumulative; preferring the latest
non-edit version keeps an `external:*` rendering layered on the upload. External
renderings are produced from the full chain below them, so an edit below an
`external:*` version is already baked in and the latest non-edit is always the
correct base. Until an external rendering exists, the base is the root. The
edit base also serves `GET /api/assets/{id}/thumbnail` when `edited=false`
(the default): the Immich web editor loads that preview as its canvas base and
re-applies the saved recipe client-side, so serving the current rendering there
would double-apply the recipe on screen. Immich web sends `edited=true` on
every other media request, which keeps the fast `asset_urls` path; the
`edited=false` path streams the base version's `version_urls` display rung.

Mock assets must set `kind` explicitly; `make_gumnut_asset` defaults it to
`"original"`.

**Immich edit routes.** `GET`/`PUT`/`DELETE /api/assets/{id}/edits` (in
`routers/api/assets.py`) adapt the unmodified Immich web editor to the version
chain. The chain stores one consolidated recipe on the current `edit` version,
not an action history. GET decodes the tip's recipe through
`recipe_to_immich_edits` — root current, an opaque (`external:*`) tip, or an
undecodable recipe all read as an empty edit list; opaque output is never
presented as adjustable. PUT normalizes the complete Immich action list against
the edit base's display dimensions (a crop must be the first action, as
upstream requires), renders via `render_asset_edit`, and
commits with `versions.append` (original current) or `versions.replace` (edit
current). Concurrency is compare-and-swap by construction: append is accepted
upstream only while the original is current, and replace/delete name the
snapshotted tip, so a tip moved by a concurrent writer returns 409 and is never
retried against a refetched chain. DELETE removes the current edit and restores
the predecessor (root current is an idempotent success; an opaque tip is 409 —
an edit-specific route must not expose a generic external-version delete).
The generated `RotateParameters` model bounds `angle` to integer values from 0
through 270, but the runtime contract is narrower: a rotate action must use
exactly `0`, `90`, `180`, or `270` degrees. Other integers within the generated
bounds are rejected with HTTP 400. Values that fail model validation return HTTP
422. The internal validation code is `invalid_angle`; the HTTP response exposes
only the explanatory message.
The emission contract for committed writes is owned by
`docs/references/websocket-events-reference.md` § `AssetEditReadyV2`.

**Suppress face geometry while an edited rendering is current.** Gumnut stores
face boxes in the original upload's pixel space, so they are invalid over
derived pixels. Gate every REST and sync emit or write site through
`should_expose_face_geometry`: REST reads return no geometry and writes return
409. Sync must transition existing rows rather than omit them — `AssetFaceV2`
emits `isVisible=false`, while `AssetFaceV1` emits `AssetFaceDeleteV1`; a later
face event after restoration re-emits the visible row. Edits and restores do
not themselves emit face events, so checkpointed clients converge only after
the next face event or full resync. Suppression does not mutate faces, people
associations, person thumbnails, or `AssetResponseDto.people`.

Register `pillow-heif` before server-side Pillow decodes; otherwise HEIC/HEIF
originals are unidentified. `services/asset_edit_renderer.py` is the pattern.
