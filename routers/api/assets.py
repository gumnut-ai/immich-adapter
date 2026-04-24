from typing import List, Literal, NamedTuple, cast
from uuid import UUID, uuid4
import base64
import logging
from datetime import datetime, timezone

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
from gumnut import AsyncGumnut, GumnutError
from gumnut.types.asset_response import AssetResponse

from config.settings import Settings, get_settings
from routers.utils.cdn_client import DEFAULT_FORWARDED_HEADERS, stream_from_cdn
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.error_mapping import (
    get_upstream_status_code,
    log_upstream_response,
    map_gumnut_error,
)
from routers.utils.current_user import get_current_user, get_current_user_id
from pydantic import ValidationError
from socketio.exceptions import SocketIOError

from services.streaming_upload import StreamingUploadPipeline
from services.websockets import emit_user_event, WebSocketEvent
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
    build_asset_upload_ready_payload,
    convert_gumnut_asset_to_immich,
    mime_type_to_asset_type,
)
from utils.livephoto import is_live_photo_video
from routers.immich_models import AssetTypeEnum

logger = logging.getLogger(__name__)

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
        variant: asset_urls key (thumbnail, preview, fullsize, original).
        range_header: Optional Range header for video seeking.
        forwarded_headers: Upstream headers to forward from CDN response.

    Returns:
        StreamingResponse streaming CDN bytes to the Immich client.
    """
    try:
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)

        asset = await client.assets.retrieve(gumnut_asset_id)

        if not asset.asset_urls or variant not in asset.asset_urls:
            logger.warning(
                "Asset variant not available",
                extra={"variant": variant, "asset_id": gumnut_asset_id},
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Asset variant '{variant}' not available",
            )

        variant_info = asset.asset_urls[variant]
        cdn_url = variant_info.url
        mimetype = variant_info.mimetype

        return await stream_from_cdn(
            cdn_url,
            mimetype,
            range_header=range_header,
            forwarded_headers=forwarded_headers,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch asset") from e


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

    try:
        results = []
        # Convert Immich checksums (hex or base64) to base64 for Gumnut
        # Build a map to avoid converting each checksum twice
        checksum_to_b64 = {
            asset.checksum: _immich_checksum_to_base64(asset.checksum)
            for asset in request.assets
        }

        existing_assets_response = await client.assets.check_existence(
            checksum_sha1s=list(checksum_to_b64.values())
        )
        existing_assets = existing_assets_response.assets

        # Build a lookup map from base64 checksum to existing asset
        b64_to_existing_asset = {
            existing_asset.checksum_sha1: existing_asset
            for existing_asset in existing_assets
            if existing_asset.checksum_sha1
        }

        for asset in request.assets:
            # Look up the pre-computed base64 checksum
            checksum_b64 = checksum_to_b64[asset.checksum]
            existing_asset = b64_to_existing_asset.get(checksum_b64)

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
                results.append(
                    {
                        "id": asset.id,
                        "action": "accept",
                    }
                )

        return AssetBulkUploadCheckResponseDto(results=results)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to check bulk upload assets") from e


@router.post("/exist")
async def check_existing_assets(
    request: CheckExistingAssetsDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> CheckExistingAssetsResponseDto:
    """
    Check if multiple assets exist on the server and return all existing.
    """
    try:
        existing_assets_response = await client.assets.check_existence(
            device_id=request.deviceId, device_asset_ids=request.deviceAssetIds
        )
        existing_ids = [
            str(safe_uuid_from_asset_id(asset.id))
            for asset in existing_assets_response.assets
        ]
    except Exception as e:
        raise map_gumnut_error(e, "Failed to check existing assets") from e

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
    except ValueError, AttributeError:
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


async def _emit_upload_events(
    gumnut_asset: AssetResponse,
    current_user: UserResponseDto,
) -> None:
    """Emit WebSocket events after a successful upload."""
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

    if use_streaming:
        return await _upload_streaming(
            request, client, current_user, settings.gumnut_api_base_url
        )
    else:
        return await _upload_buffered(request, client, current_user)


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

        # Fetch asset metadata for WebSocket events (lightweight GET, no file data)
        try:
            gumnut_asset = await client.assets.retrieve(asset_id)
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


@router.put("", status_code=204)
async def update_assets(
    request: AssetBulkUpdateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """
    Update asset metadata.
    This is a stub implementation as Gumnut does not support asset metadata updates.
    Returns HTTP 204 (No Content) as specified by the Immich API.
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", status_code=204)
async def delete_assets(
    request: AssetBulkDeleteDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user_id: UUID = Depends(get_current_user_id),
) -> Response:
    """
    Delete multiple assets using the Gumnut SDK.
    Deletes assets by their IDs. The force parameter is ignored as Gumnut handles deletion directly.
    """

    try:
        # Process each asset ID for deletion
        for asset_uuid in request.ids:
            try:
                gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)

                await client.assets.delete(gumnut_asset_id)

                # Emit WebSocket event for real-time timeline sync
                try:
                    await emit_user_event(
                        WebSocketEvent.ASSET_DELETE,
                        str(current_user_id),
                        str(asset_uuid),
                    )
                except SocketIOError as ws_error:
                    logger.warning(
                        "Failed to emit WebSocket event after asset delete",
                        extra={
                            "asset_id": str(asset_uuid),
                            "gumnut_id": str(gumnut_asset_id),
                            "error": str(ws_error),
                        },
                    )

            except GumnutError as asset_error:
                # Log individual asset errors but continue with other deletions.
                # 404 means asset is already gone; this is expected during sync.
                status_code = get_upstream_status_code(asset_error)

                if status_code == 404 or (
                    status_code is None and "not found" in str(asset_error).lower()
                ):
                    status_code = 404
                    # Asset already deleted or doesn't exist, continue
                    log_upstream_response(
                        logger,
                        context="delete_assets",
                        status_code=status_code,
                        message=f"Asset {asset_uuid} not found during deletion",
                        extra={
                            "asset_id": str(asset_uuid),
                            "gumnut_id": str(gumnut_asset_id),
                            "error": str(asset_error),
                        },
                    )
                    continue

                # For other errors, log but continue
                log_upstream_response(
                    logger,
                    context="delete_assets",
                    status_code=status_code or 500,
                    message=f"Failed to delete asset {asset_uuid}",
                    extra={
                        "asset_id": str(asset_uuid),
                        "gumnut_id": str(gumnut_asset_id),
                        "error": str(asset_error),
                    },
                )
                continue

        # Return 204 No Content on successful completion
        return Response(status_code=204)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to delete assets") from e


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

    try:
        # Get all assets from Gumnut
        gumnut_assets = client.assets.list()

        # Count assets by type
        total_assets = 0
        image_count = 0
        video_count = 0

        async for asset in gumnut_assets:
            total_assets += 1

            # Check mime_type to determine if it's an image or video
            asset_type = mime_type_to_asset_type(asset.mime_type)
            if asset_type == AssetTypeEnum.IMAGE:
                image_count += 1
            elif asset_type == AssetTypeEnum.VIDEO:
                video_count += 1
            # Note: Other types (audio, etc.) are not counted separately but are included in total

        return AssetStatsResponseDto(
            images=image_count,
            videos=video_count,
            total=total_assets,
        )

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch asset statistics") from e


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


@router.put("/{id}")
async def update_asset(
    id: UUID,
    request: UpdateAssetDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AssetResponseDto:
    """
    Update asset metadata.
    This is a stub implementation as Gumnut does not support asset metadata updates.
    Returns the asset as-is.
    """
    return await get_asset_info(id, client=client, current_user=current_user)


@router.get("/{id}")
async def get_asset_info(
    id: UUID,
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AssetResponseDto:
    try:
        gumnut_asset_id = uuid_to_gumnut_asset_id(id)

        # Retrieve the specific asset from Gumnut
        gumnut_asset = await client.assets.retrieve(gumnut_asset_id)

        # Convert Gumnut asset to AssetResponseDto format
        immich_asset = convert_gumnut_asset_to_immich(gumnut_asset, current_user)

        return immich_asset

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch asset") from e


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
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """
    Play the video for a specific asset.
    This is a stub implementation — CDN streaming worked for Immich web but
    crashes the iOS mobile client. Full video streaming support is planned.
    Returns HTTP 200 (OK) as specified by the Immich API.
    """
    return Response(status_code=status.HTTP_200_OK)


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
