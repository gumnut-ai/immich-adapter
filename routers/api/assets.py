import asyncio
from itertools import batched
from typing import Any, List, Literal, NamedTuple, cast
from uuid import UUID, uuid4
import base64
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import sentry_sdk

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    UploadFile,
    Response,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect
from gumnut import AsyncGumnut
from gumnut.types.asset_bulk_update_assets_params import Update, UpdateChange
from gumnut.types.asset_response import AssetResponse

from config.settings import Settings, get_settings
from routers.utils.cdn_client import DEFAULT_FORWARDED_HEADERS, stream_from_cdn
from routers.utils.gumnut_client import (
    BULK_CHUNK_SIZE,
    get_authenticated_gumnut_client,
)
from routers.utils.error_mapping import map_gumnut_error
from routers.utils.current_user import get_current_user, get_current_user_id
from pydantic import ValidationError

from services.streaming_upload import StreamingUploadPipeline
from services.websockets import (
    emit_user_event,
    emit_user_event_per_id,
    WebSocketEvent,
)
from routers.immich_models import (
    AssetBulkDeleteDto,
    AssetBulkUpdateDto,
    AssetBulkUploadCheckDto,
    AssetBulkUploadCheckResponseDto,
    AssetCopyDto,
    AssetJobsDto,
    AssetMediaReplaceDto,
    AssetMediaSize,
    AssetMediaResponseDto,
    AssetMediaStatus,
    AssetMetadataResponseDto,
    AssetOcrResponseDto,
    AssetMetadataUpsertDto,
    AssetResponseDto,
    AssetStatsResponseDto,
    AssetVisibility,
    CheckExistingAssetsDto,
    UserResponseDto,
    CheckExistingAssetsResponseDto,
    UpdateAssetDto,
)
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    uuid_to_gumnut_asset_id,
)
from routers.utils.asset_conversion import (
    ASSET_INCLUDE,
    ASSET_INCLUDE_NO_PEOPLE,
    build_asset_upload_ready_payload,
    convert_gumnut_asset_to_immich,
    mime_type_to_asset_type,
)
from utils.livephoto import is_live_photo_video
from routers.immich_models import AssetTypeEnum

logger = logging.getLogger(__name__)

# Non-standard status used when the client hangs up mid-upload. The response is
# never delivered (the socket is already closed) — it exists only to give the
# handler a well-defined return instead of letting ClientDisconnect escape as an
# unhandled 500.
HTTP_499_CLIENT_CLOSED_REQUEST = 499

router = APIRouter(
    prefix="/api/assets",
    tags=["assets"],
    responses={404: {"description": "Not found"}},
)


AssetVariant = Literal["thumbnail", "preview", "fullsize", "original"]

_IMMICH_SIZE_TO_VARIANT: dict[AssetMediaSize, AssetVariant] = {
    AssetMediaSize.fullsize: "fullsize",
    AssetMediaSize.preview: "preview",
    AssetMediaSize.thumbnail: "thumbnail",
}

# Variants that get an `_image` suffix for video assets.
_VIDEO_IMAGE_VARIANTS: frozenset[AssetVariant] = frozenset(
    {"thumbnail", "preview", "fullsize"}
)

# Aspect ratio (width / height) above which a landscape asset's `thumbnail`
# request is upgraded to the larger `preview` variant. The Immich web timeline
# is a justified-rows grid where every row renders at a fixed target height
# (~235px). A thumbnail is generated at 250px on its longest edge, so for a
# landscape asset that 250px is the *width* and the height is only 250/aspect
# (~140px at 16:9). The grid then upscales it to fill the row, which looks
# blurry. Serving the 1440px `preview` keeps wide-landscape cells crisp.
# 1.5 catches 16:9 and wider while leaving 4:3/3:2 on the cheap thumbnail.
# Portrait assets are unaffected: 250px lands on their height, which already
# meets the row height, so they stay sharp without the bandwidth cost. Tunable.
_LANDSCAPE_PREVIEW_ASPECT_THRESHOLD = 1.5


def _upgrade_variant_for_aspect(
    variant: AssetVariant, width: int | None, height: int | None
) -> AssetVariant:
    """Upgrade a wide-landscape `thumbnail` request to the `preview` variant.

    Only `thumbnail` requests are affected; `preview`/`fullsize`/`original`
    pass through unchanged. The upgrade applies when the asset is landscape
    (`width > height`) and its aspect ratio exceeds
    `_LANDSCAPE_PREVIEW_ASPECT_THRESHOLD` (see that constant for the rationale).
    Missing or zero dimensions (photos-api stores `0` for unknown dims) fall
    back to the requested `thumbnail` — a safe default when shape is unknown.

    `width`/`height` are display-space dims (post-rotation), so `width > height`
    means visually landscape. The returned variant still flows through
    `_resolve_variant_key`, so a video upgrade resolves to `preview_image`. This
    relies on the backend generating `preview`/`preview_image` whenever
    `thumbnail`/`thumbnail_image` exists (image variants are resized URLs of the
    same file; a video's still-image variants materialize together); otherwise
    the upgraded request would 404 instead of serving the thumbnail.
    """
    if variant != "thumbnail":
        return variant
    if not width or not height or width <= height:
        return variant
    if width / height > _LANDSCAPE_PREVIEW_ASPECT_THRESHOLD:
        return "preview"
    return variant


def _resolve_variant_key(mime_type: str, variant: AssetVariant) -> str:
    """Return the asset_urls key for the requested variant.

    Video assets expose still-image variants under `_image`-suffixed keys
    (`thumbnail_image`, `preview_image`, `fullsize_image`); images and the
    `original` variant keep the un-suffixed names.
    """
    if mime_type.startswith("video/") and variant in _VIDEO_IMAGE_VARIANTS:
        return f"{variant}_image"
    return variant


async def _retrieve_and_stream_variant(
    asset_uuid: UUID,
    client: AsyncGumnut,
    variant: AssetVariant,
    range_header: str | None = None,
    forwarded_headers: tuple[str, ...] = DEFAULT_FORWARDED_HEADERS,
) -> StreamingResponse:
    """Retrieve asset metadata and stream the requested variant from CDN.

    Args:
        asset_uuid: Immich-format asset UUID.
        client: Authenticated Gumnut client.
        variant: Logical variant name (thumbnail, preview, fullsize, original).
            For video assets the still-image variants resolve to the
            `_image`-suffixed asset_urls keys. A `thumbnail` request for a
            wide-landscape asset is upgraded to `preview` (see
            `_upgrade_variant_for_aspect`).
        range_header: Optional Range header for video seeking.
        forwarded_headers: Upstream headers to forward from CDN response.

    Returns:
        StreamingResponse streaming CDN bytes to the Immich client.
    """
    gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)
    asset = await client.assets.retrieve(gumnut_asset_id)

    variant = _upgrade_variant_for_aspect(variant, asset.width, asset.height)
    variant_key = _resolve_variant_key(asset.mime_type, variant)

    if not asset.asset_urls or variant_key not in asset.asset_urls:
        logger.warning(
            "Asset variant not available",
            extra={"variant": variant_key, "asset_id": gumnut_asset_id},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset variant '{variant_key}' not available",
        )

    variant_info = asset.asset_urls[variant_key]
    return await stream_from_cdn(
        variant_info.url,
        variant_info.mimetype,
        range_header=range_header,
        forwarded_headers=forwarded_headers,
    )


def _immich_checksum_to_base64(checksum: str) -> str:
    """
    Convert an Immich checksum (hex or base64) to base64 format for Gumnut.

    Immich clients send SHA-1 checksums as either:
    - 40-character hex strings (from web client)
    - 28-character base64 strings (from mobile clients)

    Gumnut expects base64-encoded checksums.

    Note: Invalid hex checksums are handled silently to match Immich server behavior.
    JavaScript's Buffer.from(str, 'hex') silently produces empty/garbage output for
    invalid input, so we do the same here. This results in false negatives (failing
    to detect duplicates) rather than request failures.
    """
    if len(checksum) == 28:
        # Already base64 encoded
        return checksum
    else:
        # Hex encoded - convert to base64
        try:
            checksum_bytes = bytes.fromhex(checksum)
        except ValueError as e:
            # Match Immich server behavior: invalid hex produces empty buffer
            # This will cause duplicate detection to fail silently (false negative)
            logger.warning(
                f"Invalid hex checksum '{checksum}': {e}. "
                "Returning empty checksum to match Immich server behavior."
            )
            checksum_bytes = b""
        return base64.b64encode(checksum_bytes).decode("ascii")


@router.post("/bulk-upload-check")
async def bulk_upload_check(
    request: AssetBulkUploadCheckDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> AssetBulkUploadCheckResponseDto:
    """
    Check which assets from a bulk upload already exist in Gumnut.
    """

    results = []
    # Build a map to avoid converting each checksum twice
    checksum_to_b64 = {
        asset.checksum: _immich_checksum_to_base64(asset.checksum)
        for asset in request.assets
    }

    existing_assets_response = await client.assets.check_existence(
        checksum_sha1s=list(checksum_to_b64.values())
    )

    b64_to_existing_asset = {
        existing_asset.checksum_sha1: existing_asset
        for existing_asset in existing_assets_response.assets
        if existing_asset.checksum_sha1
    }

    for asset in request.assets:
        existing_asset = b64_to_existing_asset.get(checksum_to_b64[asset.checksum])
        if existing_asset:
            results.append(
                {
                    "id": asset.id,
                    "action": "reject",
                    "reason": "duplicate",
                    "assetId": str(safe_uuid_from_asset_id(existing_asset.id)),
                    "isTrashed": False,
                }
            )
        else:
            results.append({"id": asset.id, "action": "accept"})

    return AssetBulkUploadCheckResponseDto(results=results)


@router.post("/exist")
async def check_existing_assets(
    request: CheckExistingAssetsDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> CheckExistingAssetsResponseDto:
    """
    Check if multiple assets exist on the server and return all existing.
    """
    existing_assets_response = await client.assets.check_existence(
        device_id=request.deviceId, device_asset_ids=request.deviceAssetIds
    )
    existing_ids = [
        str(safe_uuid_from_asset_id(asset.id))
        for asset in existing_assets_response.assets
    ]
    return CheckExistingAssetsResponseDto(existingIds=existing_ids)


def _parse_datetime(value: str | None, fallback: datetime) -> datetime:
    """Parse an ISO 8601 datetime string, falling back to the given default."""
    if not value:
        return fallback
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Normalize naive datetimes to match the fallback's timezone
        if dt.tzinfo is None and fallback.tzinfo is not None:
            dt = dt.replace(tzinfo=fallback.tzinfo)
        return dt
    except (ValueError, AttributeError):
        return fallback


class UploadFields(NamedTuple):
    device_asset_id: str
    device_id: str
    file_created_at: datetime
    file_modified_at: datetime


def _extract_upload_fields(fields: dict[str, str]) -> UploadFields:
    """Extract and validate common upload fields from a form data dict.

    Raises ValueError if required fields are missing.
    """
    device_asset_id = fields.get("deviceAssetId", "")
    device_id = fields.get("deviceId", "")
    file_created_at_str = fields.get("fileCreatedAt", "")

    if not device_asset_id or not device_id or not file_created_at_str:
        raise ValueError(
            "Missing required fields: deviceAssetId, deviceId, fileCreatedAt"
        )

    file_modified_at_str = fields.get("fileModifiedAt") or None
    file_created_at = _parse_datetime(file_created_at_str, datetime.now(timezone.utc))
    file_modified_at = _parse_datetime(file_modified_at_str, file_created_at)

    return UploadFields(device_asset_id, device_id, file_created_at, file_modified_at)


# Delay applied before emitting upload-success events for video uploads, to give
# photos-api time to extract the still-image variants (`thumbnail_image`,
# `preview_image`, `fullsize_image`) before the Immich web client tries to render
# the thumbnail. Image uploads emit immediately — their CDN-resized variants are
# available the moment the file is written. Tune this constant if telemetry shows
# the typical extraction time has drifted.
_VIDEO_EMIT_DELAY_SECONDS = 3.0

# Strong refs for in-flight delayed-emit tasks. asyncio only holds weak refs to
# `create_task` results; without this set the GC can collect a sleeping task.
_pending_emit_tasks: set[asyncio.Task[None]] = set()


async def _do_emit_upload_events(
    gumnut_asset: AssetResponse,
    current_user: UserResponseDto,
) -> None:
    """Emit the UPLOAD_SUCCESS + ASSET_UPLOAD_READY_V1 WebSocket events."""
    try:
        asset_response = convert_gumnut_asset_to_immich(gumnut_asset, current_user)
        await emit_user_event(
            WebSocketEvent.UPLOAD_SUCCESS, current_user.id, asset_response
        )

        payload = build_asset_upload_ready_payload(gumnut_asset, current_user.id)
        await emit_user_event(
            WebSocketEvent.ASSET_UPLOAD_READY_V1, current_user.id, payload
        )
    except Exception as ws_error:
        logger.warning(
            "Failed to emit WebSocket event after upload",
            extra={
                "gumnut_id": getattr(gumnut_asset, "id", "unknown"),
                "error": str(ws_error),
            },
        )


async def _delayed_emit_upload_events(
    gumnut_asset: AssetResponse,
    current_user: UserResponseDto,
    delay_seconds: float,
) -> None:
    """Sleep, then emit upload events. Used for videos to wait out thumbnail extraction."""
    await asyncio.sleep(delay_seconds)
    await _do_emit_upload_events(gumnut_asset, current_user)


async def _emit_upload_events(
    gumnut_asset: AssetResponse,
    current_user: UserResponseDto,
) -> None:
    """Emit WebSocket events after a successful upload.

    Images emit synchronously. Videos defer emission by `_VIDEO_EMIT_DELAY_SECONDS`
    via a detached background task — the HTTP response is not blocked, but the
    Immich web client's timeline insertion (triggered by `on_upload_success`) waits
    until video thumbnail extraction has had a chance to complete.
    """
    if gumnut_asset.mime_type.startswith("video/"):
        task = asyncio.create_task(
            _delayed_emit_upload_events(
                gumnut_asset, current_user, _VIDEO_EMIT_DELAY_SECONDS
            )
        )
        _pending_emit_tasks.add(task)
        task.add_done_callback(_pending_emit_tasks.discard)
        return

    await _do_emit_upload_events(gumnut_asset, current_user)


@router.post(
    "",
    status_code=201,
    response_model=AssetMediaResponseDto,
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {"$ref": "#/components/schemas/AssetMediaCreateDto"}
                }
            }
        }
    },
    responses={
        200: {
            "model": AssetMediaResponseDto,
            "description": "Duplicate asset detected",
        }
    },
)
async def upload_asset(
    request: Request,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> AssetMediaResponseDto | JSONResponse:
    """
    Upload an asset using the Gumnut SDK.
    Creates a new asset in Gumnut from the provided asset data.
    Returns 201 on success, 200 if the asset is a duplicate.

    Uses a dual-path strategy:
    - Small files (below threshold): buffered via Starlette's UploadFile
    - Large files (above threshold): streamed directly to photos-api
    """
    threshold = settings.streaming_upload_threshold_bytes

    raw_cl = request.headers.get("content-length")
    try:
        content_length: int | None = int(raw_cl) if raw_cl is not None else None
    except ValueError:
        content_length = None

    # Only stream when we know the size exceeds the threshold (or threshold is 0
    # to force streaming). Missing/invalid Content-Length and chunked transfers
    # fall through to the buffered path to preserve live photo detection, which
    # requires file seeks incompatible with streaming.
    use_streaming = threshold == 0 or (
        content_length is not None and content_length > threshold
    )

    strategy = "streaming" if use_streaming else "buffered"
    logger.info(
        "Upload strategy: %s",
        strategy,
        extra={
            "strategy": strategy,
            "content_length": content_length,
            "threshold": threshold,
        },
    )

    try:
        if use_streaming:
            return await _upload_streaming(
                request, client, current_user, settings.gumnut_api_base_url
            )
        else:
            return await _upload_buffered(request, client, current_user)
    except ClientDisconnect:
        # The client hung up before finishing the upload (mobile backgrounding,
        # cancel, network blip). The connection is gone, so there's no one to
        # receive a response — treat it as a normal aborted upload rather than
        # letting it surface as an unhandled 500.
        logger.info(
            "Client disconnected during %s upload before it completed",
            strategy,
            extra={"strategy": strategy},
        )
        return JSONResponse(
            content={"detail": "Client disconnected before upload completed"},
            status_code=HTTP_499_CLIENT_CLOSED_REQUEST,
        )


async def _upload_buffered(
    request: Request,
    client: AsyncGumnut,
    current_user: UserResponseDto,
) -> AssetMediaResponseDto | JSONResponse:
    """Standard buffered upload path — Starlette spools file to /tmp."""
    async with request.form() as form:
        asset_data_raw = form.get("assetData")
        # Duck-type check: Starlette's UploadFile may not pass isinstance against
        # FastAPI's UploadFile in all environments, so fall back to attribute check.
        if not (
            hasattr(asset_data_raw, "filename")
            and getattr(asset_data_raw, "filename", None)
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="File has no filename",
            )
        asset_data = cast(UploadFile, asset_data_raw)

        # Convert Starlette form values to plain strings for shared helper
        fields = {key: str(value) for key, value in form.items() if key != "assetData"}
        try:
            device_asset_id, device_id, file_created_at, file_modified_at = (
                _extract_upload_fields(fields)
            )
        except ValueError as ve:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(ve),
            )

        try:
            # Drop iOS live photo .MOV files — they upload as separate video files
            # that would become orphan assets since Gumnut doesn't support live photos.
            filename_lower = (asset_data.filename or "").lower()
            may_be_video = (
                asset_data.content_type and asset_data.content_type.startswith("video/")
            ) or filename_lower.endswith((".mov", ".mp4", ".m4v"))
            if may_be_video and is_live_photo_video(asset_data.file):
                logger.info(
                    "Dropping iOS live photo video",
                    extra={
                        "device_asset_id": device_asset_id,
                        "device_id": device_id,
                        "upload_filename": asset_data.filename,
                        "content_type": asset_data.content_type,
                    },
                )
                return AssetMediaResponseDto(
                    id=str(uuid4()),
                    status=AssetMediaStatus.created,
                )

            await asset_data.seek(0)
            with sentry_sdk.start_span(
                op="http.client", name="gumnut.assets.create"
            ) as span:
                span.set_data("upload.filename", asset_data.filename)
                span.set_data("upload.content_type", asset_data.content_type)
                span.set_data("upload.strategy", "buffered")
                # Use with_raw_response to access the HTTP status code:
                # photos-api returns 200 for duplicates, 201 for new assets,
                # but the SDK parses both into the same AssetResponse type.
                raw_response = await client.assets.with_raw_response.create(
                    asset_data=(
                        asset_data.filename,
                        asset_data.file,
                        asset_data.content_type,
                    ),
                    device_asset_id=device_asset_id,
                    device_id=device_id,
                    file_created_at=file_created_at,
                    file_modified_at=file_modified_at,
                )

            gumnut_asset = await raw_response.parse()
            asset_uuid = safe_uuid_from_asset_id(gumnut_asset.id)

            if raw_response.status_code == status.HTTP_200_OK:
                return JSONResponse(
                    content={
                        "id": str(asset_uuid),
                        "status": AssetMediaStatus.duplicate.value,
                    },
                    status_code=status.HTTP_200_OK,
                )

            await _emit_upload_events(gumnut_asset, current_user)

            return AssetMediaResponseDto(
                id=str(asset_uuid), status=AssetMediaStatus.created
            )

        except Exception as e:
            raise map_gumnut_error(
                e,
                "Failed to upload asset",
                extra={
                    "upload_filename": asset_data.filename,
                    "content_type": asset_data.content_type,
                    "device_asset_id": device_asset_id,
                    "device_id": device_id,
                    "strategy": "buffered",
                },
                exc_info=True,
            ) from e


async def _upload_streaming(
    request: Request,
    client: AsyncGumnut,
    current_user: UserResponseDto,
    api_base_url: str,
) -> AssetMediaResponseDto | JSONResponse:
    """Streaming upload path — pipes file data to photos-api without buffering.

    Note: requires multipart form fields (deviceAssetId, deviceId, fileCreatedAt)
    to precede the file part. All known Immich clients send fields first. Clients
    that send the file before fields will receive a 422 error; those uploads fall
    below the streaming threshold in practice, so they use the buffered path.
    """
    jwt_token = getattr(request.state, "jwt_token", None)
    if not jwt_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    pipeline: StreamingUploadPipeline | None = None
    try:
        pipeline = StreamingUploadPipeline(request, api_base_url, jwt_token)
        result = await pipeline.execute(_extract_upload_fields)

        asset_id = result.get("id", "")

        if pipeline.last_status_code is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Streaming pipeline did not capture upstream status code",
            )
        http_status = pipeline.last_status_code

        if http_status == status.HTTP_200_OK:
            if not asset_id:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Duplicate response from upstream missing asset ID",
                )
            dup_uuid = safe_uuid_from_asset_id(asset_id)
            return JSONResponse(
                content={
                    "id": str(dup_uuid),
                    "status": AssetMediaStatus.duplicate.value,
                },
                status_code=status.HTTP_200_OK,
            )

        asset_uuid = safe_uuid_from_asset_id(asset_id)

        # Fetch asset metadata for WebSocket events (lightweight GET, no image bytes)
        try:
            gumnut_asset = await client.assets.retrieve(asset_id, include=ASSET_INCLUDE)
            await _emit_upload_events(gumnut_asset, current_user)
        except Exception as ws_err:
            logger.warning(
                "Failed to emit WebSocket events for streaming upload",
                extra={"asset_id": asset_id, "error": str(ws_err)},
            )

        return AssetMediaResponseDto(
            id=str(asset_uuid), status=AssetMediaStatus.created
        )

    except HTTPException:
        raise
    except ClientDisconnect:
        # Let the disconnect propagate to upload_asset's handler instead of
        # mapping it to a 500/502 — the client is already gone.
        raise
    except (ValueError, ValidationError) as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        )
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT, detail="Upload timed out"
        )
    except httpx.HTTPError as e:
        logger.error(
            "Streaming upload connection error",
            extra={"error": str(e), "strategy": "streaming"},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Upload failed"
        )
    except Exception as e:
        log_extra = {"strategy": "streaming"}
        if pipeline is not None:
            form_parser = pipeline.form_parser
            if form_parser.filename:
                log_extra["upload_filename"] = form_parser.filename
            if form_parser.content_type:
                log_extra["content_type"] = form_parser.content_type
            if device_asset_id := form_parser.form_fields.get("deviceAssetId"):
                log_extra["device_asset_id"] = device_asset_id
            if device_id := form_parser.form_fields.get("deviceId"):
                log_extra["device_id"] = device_id

        raise map_gumnut_error(
            e,
            "Failed to upload asset",
            extra=log_extra,
            exc_info=True,
        ) from e


def _resolve_timezone(tz_name: str) -> ZoneInfo:
    """Resolve an IANA timezone name, mapping bad input to 422.

    `ZoneInfo()` raises `ValueError` for malformed keys (empty string,
    absolute paths, non-normalized paths like "../..") and
    `ZoneInfoNotFoundError` for well-formed-but-unknown zones; both must
    surface as the 422 the rest of this module uses rather than an uncaught
    500.
    """
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid timeZone: {tz_name!r}",
        ) from exc


def _combine_datetime_with_timezone(dt: datetime, tz_name: str) -> datetime:
    """Apply an IANA timezone to a parsed `dateTimeOriginal`.

    The Immich bulk DTO carries `timeZone` as an IANA name (e.g.
    `America/Los_Angeles`). The Photos API encodes the offset directly into
    `original_datetime`, so we localize wall-clock here. Aware inputs are
    re-anchored: the wall-clock components are preserved and re-tagged with
    the new tz, matching Immich's "interpret these clock digits in this zone"
    UX (the modal sends a naive `dateTimeOriginal` when `timeZone` is set,
    but we handle aware too for safety).
    """
    return dt.replace(tzinfo=_resolve_timezone(tz_name))


class _RelativeShift(NamedTuple):
    """Shift each asset's existing `original_datetime` by a fixed number of
    seconds (`dateTimeRelative`)."""

    relative_seconds: float

    def apply(self, current: datetime) -> datetime:
        return current + timedelta(seconds=self.relative_seconds)


class _ReinterpretTimezone(NamedTuple):
    """Re-anchor each asset's existing wall-clock to a zone (standalone
    `timeZone`): preserve the clock digits, swap the tzinfo, exactly as
    `_combine_datetime_with_timezone` does for the absolute path."""

    tz: ZoneInfo

    def apply(self, current: datetime) -> datetime:
        return current.replace(tzinfo=self.tz)


# A per-asset `original_datetime` rewrite that depends on each asset's current
# value, so the handler must read the batch before writing. A tagged union so
# each mode's field is non-optional — the "exactly one mode" invariant lives in
# the type rather than a runtime assert.
_PerAssetDatetime = _RelativeShift | _ReinterpretTimezone


class _BulkMetadataChange(NamedTuple):
    """The two inputs a bulk update is split into.

    - `base` — fields identical across every id (homogeneous); may be empty.
    - `transform` — an optional per-asset `original_datetime` rewrite that needs
      each asset's current value read first, or `None`.
    """

    base: dict[str, Any]
    transform: _PerAssetDatetime | None


def _build_bulk_metadata_change(
    request: AssetBulkUpdateDto,
) -> _BulkMetadataChange:
    """Split an `AssetBulkUpdateDto` into the two bulk-update inputs.

    Returns a `_BulkMetadataChange(base, transform)`:

    - `base` — fields identical across every id (homogeneous):
      `description`, paired `latitude` + `longitude`, and an absolute
      `original_datetime` (from `dateTimeOriginal`, optionally localized by a
      paired `timeZone`). May be empty.
    - `transform` — a per-asset `original_datetime` rewrite that needs each
      asset's current value read first (`dateTimeRelative`, or standalone
      `timeZone` without `dateTimeOriginal`), or `None`. See
      `_PerAssetDatetime`.

    Mirrors `_build_metadata_patch` for the single-asset path: uses
    `model_fields_set` to distinguish "field omitted" from "field explicitly
    null" and validates paired lat/lon adapter-side. Explicit `null` for
    `dateTimeRelative` / `timeZone` is treated as "field not set" — clients
    send null for fields they don't intend to change — so the per-asset
    rewrites only trigger on non-null values.

    Out-of-scope DTO fields (`isFavorite`, `rating`, `visibility`,
    `duplicateId`) are silently ignored — the request still succeeds, the
    adapter just doesn't act on parts the Photos API doesn't model.

    The three datetime modes (absolute `dateTimeOriginal`, relative
    `dateTimeRelative`, standalone `timeZone` reinterpret) are mutually
    exclusive; combining them is rejected with 422 since the intended result
    would be ambiguous.
    """
    provided = request.model_fields_set

    has_absolute_dt = "dateTimeOriginal" in provided
    relative = request.dateTimeRelative if "dateTimeRelative" in provided else None
    tz_name = request.timeZone if "timeZone" in provided else None

    transform: _PerAssetDatetime | None = None
    base: dict[str, Any] = {}

    # The three datetime modes are mutually exclusive.
    if relative is not None and has_absolute_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="dateTimeRelative and dateTimeOriginal cannot be combined",
        )
    if relative is not None and tz_name is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="dateTimeRelative and timeZone cannot be combined",
        )

    if relative is not None:
        # Per-asset second-shift; needs each asset's current datetime.
        transform = _RelativeShift(relative_seconds=relative)
    elif tz_name is not None and not has_absolute_dt:
        # Standalone timeZone reinterprets each asset's existing wall-clock;
        # resolve the zone eagerly so a bad name 422s before any read.
        transform = _ReinterpretTimezone(tz=_resolve_timezone(tz_name))
    elif has_absolute_dt:
        # Absolute datetime (optionally localized) is the same for every id.
        parsed = _parse_update_original_datetime(request.dateTimeOriginal)
        if parsed is not None and tz_name is not None:
            parsed = _combine_datetime_with_timezone(parsed, tz_name)
        base["original_datetime"] = parsed

    if "description" in provided:
        base["description"] = request.description

    lat_set = "latitude" in provided
    lon_set = "longitude" in provided
    if lat_set != lon_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="latitude and longitude must be provided together",
        )
    if lat_set and lon_set:
        if (request.latitude is None) != (request.longitude is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="latitude and longitude must be cleared together",
            )
        base["latitude"] = request.latitude
        base["longitude"] = request.longitude

    return _BulkMetadataChange(base, transform)


@router.put("", status_code=204)
async def update_assets(
    request: AssetBulkUpdateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """Bulk-update asset metadata.

    Wires Immich's `PUT /api/assets` to the Photos API `bulk_update_assets`
    call, which accepts heterogeneous per-item `change` dicts.

    In-scope fields: `description`, paired `latitude` + `longitude`, and the
    capture time via one of three mutually exclusive datetime modes:

    - absolute `dateTimeOriginal` (optionally localized by a paired
      `timeZone`) — identical for every id, so it's replicated as a single
      homogeneous `change`;
    - `dateTimeRelative` — a per-asset second-shift; and
    - standalone `timeZone` — reinterpret each asset's existing wall-clock in
      the given zone.

    The two per-asset modes need each asset's *current* `original_datetime`,
    so the handler reads the chunk first (`client.assets.list(state="all",
    ids=...)`) and builds a per-item `change`. This is the "bulk GET + bulk
    PATCH" shape: one read and one write per chunk, not a fan-out of per-asset
    GETs. `state="all"` so trashed assets are read too: the homogeneous path
    forwards every id to `bulk_update_assets` regardless of trash state, and the
    default live-only filter would otherwise silently drop trashed ids from the
    read — leaving the per-asset modes asymmetrically skipping them. Assets with
    no existing `original_datetime` (and ids not returned by the read) are
    skipped for the datetime rewrite — if they carry no other in-scope field
    they drop out of the write entirely. There is a small read-then-write
    window in which an asset's datetime could change between the two calls.

    Out-of-scope DTO fields (`isFavorite`, `rating`, `visibility`,
    `duplicateId`) are silently ignored. Conflicting datetime modes are
    rejected with 422 — see `_build_bulk_metadata_change`.

    No WebSocket events are emitted on success: `bulk_update_assets` returns
    no per-asset payload, so we don't have post-update assets to mirror the
    single-asset path's `ASSET_UPDATE` payload cheaply (the per-asset read
    above is pre-update state for the datetime modes only). Clients fall back
    to refresh on next sync.

    The SDK caps each call at `BULK_CHUNK_SIZE` (100) items, so requests over
    that are split into chunks. The SDK guarantees per-call atomicity (a
    single chunk either fully commits or writes nothing), but that guarantee
    does not extend across chunks: a failure on chunk N (N ≥ 2) leaves chunks
    1..N-1 already committed, with no compensating rollback and no per-chunk
    error report — the exception propagates as one 5xx. Consistent with
    `_bulk_permanent_delete` / `_bulk_trash`.
    """
    if not request.ids:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    base_change, transform = _build_bulk_metadata_change(request)
    if not base_change and transform is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    for chunk in batched(request.ids, BULK_CHUNK_SIZE):
        gumnut_ids = [uuid_to_gumnut_asset_id(uid) for uid in chunk]
        updates: list[Update]

        if transform is None:
            sdk_change = cast(UpdateChange, base_change)
            updates = [{"id": gid, "change": sdk_change} for gid in gumnut_ids]
        else:
            # Per-asset datetime rewrite: read current values, then write.
            # state="all" so trashed assets aren't silently dropped from the
            # read (and thus the rewrite) — the homogeneous path above forwards
            # them regardless, so the per-asset path must too.
            page = await client.assets.list(
                state="all",
                ids=gumnut_ids,
                limit=len(gumnut_ids),
                include=ASSET_INCLUDE_NO_PEOPLE,
            )
            current_by_id = {
                asset.id: asset.metadata.original_datetime
                for asset in page.data
                if asset.metadata is not None
            }
            updates = []
            for gid in gumnut_ids:
                change = dict(base_change)
                current = current_by_id.get(gid)
                if current is not None:
                    change["original_datetime"] = transform.apply(current)
                if change:
                    updates.append({"id": gid, "change": cast(UpdateChange, change)})
            if not updates:
                continue

        await client.assets.bulk_update_assets(updates=updates)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", status_code=204)
async def delete_assets(
    request: AssetBulkDeleteDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user_id: UUID = Depends(get_current_user_id),
) -> Response:
    """
    Delete multiple assets, branching on the Immich `force` flag.

    - ``force=True`` permanently deletes via the backend's bulk
      ``DELETE /api/assets`` endpoint. Emits one ``on_asset_delete`` per id —
      Immich's wire shape is single-id-per-event for permanent deletes.
    - ``force=False`` or absent (Immich's native default) soft-deletes via the
      backend's ``POST /api/assets/trash`` endpoint. Emits a single batched
      ``on_asset_trash`` event per chunk carrying the full id array.

    The backend bulk endpoints are idempotent — already-trashed or already-purged
    rows are skipped without erroring — so the previous per-id 404 swallowing
    is not needed. Bulk failures (validation, transport, 5xx) propagate to the
    client via the global ``GumnutError`` handler.
    """
    if not request.ids:
        return Response(status_code=204)

    if request.force:
        await _bulk_permanent_delete(client, request.ids, str(current_user_id))
    else:
        await _bulk_trash(client, request.ids, str(current_user_id))

    return Response(status_code=204)


async def _bulk_permanent_delete(
    client: AsyncGumnut,
    asset_uuids: list[UUID],
    user_id: str,
) -> None:
    """Bulk hard-delete; emits one on_asset_delete per id."""
    for chunk in batched(asset_uuids, BULK_CHUNK_SIZE):
        gumnut_ids = [uuid_to_gumnut_asset_id(uid) for uid in chunk]
        await client.delete(
            "/api/assets",
            body={"ids": gumnut_ids},
            cast_to=type(None),
        )
        await emit_user_event_per_id(
            WebSocketEvent.ASSET_DELETE,
            user_id,
            (str(asset_uuid) for asset_uuid in chunk),
        )


async def _bulk_trash(
    client: AsyncGumnut,
    asset_uuids: list[UUID],
    user_id: str,
) -> None:
    """Bulk soft-delete; emits one batched on_asset_trash per chunk."""
    for chunk in batched(asset_uuids, BULK_CHUNK_SIZE):
        gumnut_ids = [uuid_to_gumnut_asset_id(uid) for uid in chunk]
        await client.post(
            "/api/assets/trash",
            body={"ids": gumnut_ids},
            cast_to=type(None),
        )
        chunk_uuid_strs = [str(uid) for uid in chunk]
        await emit_user_event(
            WebSocketEvent.ASSET_TRASH,
            user_id,
            chunk_uuid_strs,
        )


@router.get("/device/{deviceId}", deprecated=True)
async def get_all_user_assets_by_device_id(
    deviceId: str,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[str]:
    """
    Retrieve assets by device ID.
    This is a stub implementation as Gumnut does not support querying by device ID directly.
    Returns an empty list.
    """
    return []


@router.get("/statistics")
async def get_asset_statistics(
    isFavorite: bool = Query(default=None, alias="isFavorite"),
    isTrashed: bool = Query(default=None, alias="isTrashed"),
    visibility: AssetVisibility = Query(default=None, alias="visibility"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> AssetStatsResponseDto:
    """
    Get asset statistics from Gumnut.
    Counts total assets and categorizes them by type (images vs videos) using mime_type.
    """

    gumnut_assets = (
        client.assets.list(state="trashed") if isTrashed else client.assets.list()
    )

    total_assets = 0
    image_count = 0
    video_count = 0

    async for asset in gumnut_assets:
        total_assets += 1
        asset_type = mime_type_to_asset_type(asset.mime_type)
        if asset_type == AssetTypeEnum.IMAGE:
            image_count += 1
        elif asset_type == AssetTypeEnum.VIDEO:
            video_count += 1

    return AssetStatsResponseDto(
        images=image_count,
        videos=video_count,
        total=total_assets,
    )


@router.get("/random", deprecated=True)
async def get_random(
    count: int = Query(default=None, ge=1, type="number"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[AssetResponseDto]:
    """
    Get random assets.
    This is a stub implementation that returns an empty list.
    Deprecated in v1.116.0 - use search endpoint instead.
    """
    # Stub implementation: return empty list since this endpoint is deprecated
    return []


@router.post("/jobs", status_code=204)
async def run_asset_jobs(
    request: AssetJobsDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Run asset jobs.
    This is a stub implementation as Gumnut does not support running asset jobs.
    Returns HTTP 204 (No Content) as specified by the Immich API.
    """
    # Stub implementation: asset jobs are not supported in Gumnut
    return Response(status_code=204)


@router.put("/copy", status_code=204)
async def copy_asset(
    request: AssetCopyDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Copy asset metadata between assets.
    This is a stub implementation as Gumnut does not support copying asset metadata.
    Returns HTTP 204 (No Content) as specified by the Immich API.
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _parse_update_original_datetime(value: str | None) -> datetime | None:
    """Parse the `dateTimeOriginal` field on an `UpdateAssetDto`.

    Distinct from `_parse_datetime` (which substitutes a fallback on parse
    failure for upload paths) — here an invalid input must surface as 422.
    A `None` value means "clear" and is passed through.
    """
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid dateTimeOriginal: {value!r}",
        ) from exc


def _build_metadata_patch(request: UpdateAssetDto) -> dict[str, Any] | None:
    """Build the SDK `update_asset` kwargs from an `UpdateAssetDto`.

    Uses `model_fields_set` to distinguish "field omitted" from "field
    explicitly null" — both look like `None` on the model because the DTO
    defaults every field to `None`. Out-of-scope DTO fields
    (`isFavorite`, `rating`, `visibility`, `livePhotoVideoId`) are silently
    ignored; the request still succeeds, we just don't act on parts the
    Photos API doesn't model. Returns `None` when no in-scope fields were
    set, signalling the caller to skip the PATCH entirely.

    Validates paired lat/lon adapter-side so the request 422s before the
    network call when the client sends half-set or half-cleared coords.
    """
    provided = request.model_fields_set
    patch: dict[str, Any] = {}

    if "description" in provided:
        patch["description"] = request.description

    lat_set = "latitude" in provided
    lon_set = "longitude" in provided
    if lat_set != lon_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="latitude and longitude must be provided together",
        )
    if lat_set and lon_set:
        if (request.latitude is None) != (request.longitude is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="latitude and longitude must be cleared together",
            )
        patch["latitude"] = request.latitude
        patch["longitude"] = request.longitude

    if "dateTimeOriginal" in provided:
        patch["original_datetime"] = _parse_update_original_datetime(
            request.dateTimeOriginal
        )

    return patch or None


@router.put("/{id}")
async def update_asset(
    id: UUID,
    request: UpdateAssetDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AssetResponseDto:
    """Update single-asset metadata.

    Wires Immich's `PUT /api/assets/{id}` to the Photos API
    `update_asset` PATCH. In-scope DTO fields: `description`,
    `latitude` + `longitude`, `dateTimeOriginal`. Out-of-scope fields
    (`isFavorite`, `rating`, `visibility`, `livePhotoVideoId`) are
    silently ignored — the request still succeeds, but the adapter
    doesn't act on parts the Photos API doesn't model.
    """
    payload = _build_metadata_patch(request)
    if payload is None:
        return await get_asset_info(id, client=client, current_user=current_user)

    gumnut_asset = await client.assets.update_asset(
        uuid_to_gumnut_asset_id(id), **payload
    )
    immich_asset = convert_gumnut_asset_to_immich(gumnut_asset, current_user)
    await emit_user_event(WebSocketEvent.ASSET_UPDATE, current_user.id, immich_asset)
    return immich_asset


@router.get("/{id}")
async def get_asset_info(
    id: UUID,
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AssetResponseDto:
    gumnut_asset_id = uuid_to_gumnut_asset_id(id)
    gumnut_asset = await client.assets.retrieve(gumnut_asset_id, include=ASSET_INCLUDE)
    return convert_gumnut_asset_to_immich(gumnut_asset, current_user)


@router.get(
    "/{id}/thumbnail",
    responses={
        200: {
            "description": "Any binary media",
            "content": {
                "image/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
                "video/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
                "*/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
            },
        }
    },
)
async def view_asset(
    id: UUID,
    size: AssetMediaSize = Query(default=None, alias="size"),
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> StreamingResponse:
    """
    Get a thumbnail for an asset.
    Retrieves asset metadata and streams the requested variant from CDN.
    """
    preferred_size = size if size is not None else AssetMediaSize.thumbnail
    variant = _IMMICH_SIZE_TO_VARIANT.get(preferred_size, "thumbnail")
    return await _retrieve_and_stream_variant(id, client, variant)


@router.get(
    "/{id}/original",
    responses={
        200: {
            "description": "Any binary media",
            "content": {
                "image/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
                "video/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
                "*/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
            },
        }
    },
)
async def download_asset(
    id: UUID,
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> StreamingResponse:
    """
    Download the original asset file.

    Fetches the original variant from CDN, preserving the original format
    (JPEG, HEIC, RAW, etc.) for actual downloads.
    """
    return await _retrieve_and_stream_variant(
        id,
        client,
        "original",
        forwarded_headers=DEFAULT_FORWARDED_HEADERS + ("content-disposition",),
    )


@router.put(
    "/{id}/original",
    deprecated=True,
    response_model=AssetMediaResponseDto,
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {"$ref": "#/components/schemas/AssetMediaReplaceDto"}
                }
            }
        }
    },
)
async def replace_asset(
    id: UUID,
    request: AssetMediaReplaceDto,
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """
    Replace the asset with new file, without changing its id.
    Deprecated in immich and not supported by Gumnut.
    """
    return


@router.get("/{id}/metadata")
async def get_asset_metadata(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[AssetMetadataResponseDto]:
    """
    Retrieve metadata for a specific asset.
    This is a stub implementation as Gumnut does not support querying asset metadata.
    Returns an empty array.
    """
    return []


@router.put("/{id}/metadata")
async def update_asset_metadata(
    id: UUID,
    request: AssetMetadataUpsertDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[AssetMetadataResponseDto]:
    """
    Update metadata for a specific asset.
    This is a stub implementation as Gumnut does not support updating asset metadata.
    Returns an empty array.
    """
    return []


@router.delete("/{id}/metadata/{key}", status_code=204)
async def delete_asset_metadata(
    id: UUID,
    key: str,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """
    Delete a specific metadata key for an asset.
    This is a stub implementation as Gumnut does not support deleting asset metadata.
    Returns an empty object.
    """
    return


@router.get("/{id}/metadata/{key}", response_model=AssetMetadataResponseDto)
async def get_asset_metadata_by_key(
    id: UUID,
    key: str,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """
    Retrieve a specific metadata key for an asset.
    This is a stub implementation as Gumnut does not support querying asset metadata.
    Returns an empty object.
    """
    return


@router.get("/{id}/video/playback")
async def play_asset_video(
    id: UUID,
    request: Request,
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> StreamingResponse:
    """
    Play the video for a specific asset.

    Streams the original video variant from CDN. Forwards the client's Range
    header for seeking; `stream_from_cdn` advertises `Accept-Ranges: bytes` on
    the initial 200 response so iOS AVPlayer treats the source as seekable.
    """
    range_header = request.headers.get("range")
    return await _retrieve_and_stream_variant(
        id, client, "original", range_header=range_header
    )


@router.get("/{id}/ocr")
async def get_asset_ocr(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> list[AssetOcrResponseDto]:
    """
    Retrieve OCR data for an asset.
    This is a stub implementation as Gumnut does not support OCR.
    Returns an empty list.
    """
    return []
