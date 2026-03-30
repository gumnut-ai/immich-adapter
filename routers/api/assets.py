from typing import List
from uuid import UUID, uuid4
import base64
import logging
from datetime import datetime

import sentry_sdk

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    UploadFile,
    File,
    Form,
    Query,
    Response,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from gumnut import AsyncGumnut, GumnutError

from routers.utils.cdn_client import stream_from_cdn
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.error_mapping import map_gumnut_error, check_for_error_by_code
from routers.utils.current_user import get_current_user, get_current_user_id
from pydantic import ValidationError
from socketio.exceptions import SocketIOError

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


_IMMICH_SIZE_TO_VARIANT: dict[AssetMediaSize, str] = {
    AssetMediaSize.fullsize: "fullsize",
    AssetMediaSize.preview: "preview",
    AssetMediaSize.thumbnail: "thumbnail",
}


async def _retrieve_and_stream_variant(
    asset_uuid: UUID,
    client: AsyncGumnut,
    variant: str,
    range_header: str | None = None,
) -> StreamingResponse:
    """Retrieve asset metadata and stream the requested variant from CDN.

    Args:
        asset_uuid: Immich-format asset UUID.
        client: Authenticated Gumnut client.
        variant: asset_urls key (thumbnail, preview, fullsize, original).
        range_header: Optional Range header for video seeking.

    Returns:
        StreamingResponse streaming CDN bytes to the Immich client.
    """
    try:
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)

        asset = await client.assets.retrieve(gumnut_asset_id)

        if not asset.asset_urls or variant not in asset.asset_urls:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Asset variant '{variant}' not available",
            )

        variant_info = asset.asset_urls[variant]
        cdn_url = variant_info.url
        mimetype = variant_info.mimetype

        return await stream_from_cdn(cdn_url, mimetype, range_header=range_header)

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
    assetData: UploadFile = File(...),
    deviceAssetId: str = Form(...),
    deviceId: str = Form(...),
    fileCreatedAt: str = Form(...),
    fileModifiedAt: str = Form(None),
    isFavorite: bool = Form(False),
    duration: str = Form(None),
    key: str = Query(default=None),
    slug: str = Query(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AssetMediaResponseDto | JSONResponse:
    """
    Upload an asset using the Gumnut SDK.
    Creates a new asset in Gumnut from the provided asset data.
    Returns 201 on success, 200 if the asset is a duplicate.
    """

    try:
        # Parse datetime from form data
        try:
            file_created_at = datetime.fromisoformat(
                fileCreatedAt.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            file_created_at = datetime.now()

        file_modified_at = file_created_at
        if fileModifiedAt:
            try:
                file_modified_at = datetime.fromisoformat(
                    fileModifiedAt.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                file_modified_at = file_created_at

        # Drop iOS live photo .MOV files — they upload as separate video files
        # that would become orphan assets since Gumnut doesn't support live photos.
        # The Immich mobile client sends .MOV files with content_type
        # "application/octet-stream", so we check both the content type and the
        # file extension to determine if this might be a video.
        filename_lower = (assetData.filename or "").lower()
        may_be_video = (
            assetData.content_type and assetData.content_type.startswith("video/")
        ) or filename_lower.endswith((".mov", ".mp4", ".m4v"))
        if may_be_video and is_live_photo_video(assetData.file):
            logger.info(
                "Dropping iOS live photo video",
                extra={
                    "device_asset_id": deviceAssetId,
                    "device_id": deviceId,
                    "upload_filename": assetData.filename,
                    "content_type": assetData.content_type,
                },
            )
            # Unique ID for the dropped asset. Currently unused by the Immich
            # mobile client, but included for future safety so responses
            # don't collide.
            return AssetMediaResponseDto(
                id=str(uuid4()),
                status=AssetMediaStatus.created,
            )

        # Stream the file to Gumnut SDK without loading into memory.
        await assetData.seek(0)
        with sentry_sdk.start_span(
            op="http.client", name="gumnut.assets.create"
        ) as span:
            span.set_data("upload.filename", assetData.filename)
            span.set_data("upload.content_type", assetData.content_type)
            span.set_data("upload.device_asset_id", deviceAssetId)
            span.set_data("upload.device_id", deviceId)
            gumnut_asset = await client.assets.create(
                asset_data=(
                    assetData.filename,
                    assetData.file,
                    assetData.content_type,
                ),
                device_asset_id=deviceAssetId,
                device_id=deviceId,
                file_created_at=file_created_at,
                file_modified_at=file_modified_at,
            )
            span.set_data("upload.gumnut_asset_id", gumnut_asset.id)

        # Get the asset ID from the AssetResponse
        asset_id = gumnut_asset.id

        # Convert to UUID format for response
        asset_uuid = safe_uuid_from_asset_id(asset_id)

        # Emit WebSocket events for real-time updates
        try:
            # Build AssetResponseDto for on_upload_success event
            asset_response = convert_gumnut_asset_to_immich(gumnut_asset, current_user)
            await emit_user_event(
                WebSocketEvent.UPLOAD_SUCCESS, current_user.id, asset_response
            )

            # Build payload for AssetUploadReadyV1 event
            payload = build_asset_upload_ready_payload(gumnut_asset, current_user.id)
            await emit_user_event(
                WebSocketEvent.ASSET_UPLOAD_READY_V1, current_user.id, payload
            )
        except (ValidationError, SocketIOError) as ws_error:
            logger.warning(
                "Failed to emit WebSocket event after upload",
                extra={
                    "gumnut_id": str(asset_id),
                    "immich_id": str(asset_uuid),
                    "error": str(ws_error),
                },
            )

        return AssetMediaResponseDto(
            id=str(asset_uuid), status=AssetMediaStatus.created
        )

    except Exception as e:
        logger.warning(
            "Upload failed",
            extra={
                "file_name": assetData.filename,
                "content_type": assetData.content_type,
                "device_asset_id": deviceAssetId,
                "device_id": deviceId,
                "error": str(e),
            },
            exc_info=True,
        )
        # Handle specific upload error cases first
        error_msg = str(e).lower()
        if "duplicate" in error_msg or "already exists" in error_msg:
            # If it's a duplicate, we still need an asset ID
            # This is a simplified approach - in a real implementation you'd extract the existing asset ID
            # Return 200 (not 201) for duplicates, matching Immich server behavior
            return JSONResponse(
                content={
                    "id": "00000000-0000-0000-0000-000000000000",
                    "status": AssetMediaStatus.duplicate.value,
                },
                status_code=status.HTTP_200_OK,
            )
        elif check_for_error_by_code(e, 413) or "too large" in error_msg:
            raise HTTPException(status_code=413, detail="Asset file too large")
        elif check_for_error_by_code(e, 415) or "unsupported" in error_msg:
            raise HTTPException(status_code=415, detail="Unsupported media type")
        else:
            # Use the general error mapper for other cases
            raise map_gumnut_error(e, "Failed to upload asset") from e


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
                # Log individual asset errors but continue with other deletions
                if (
                    check_for_error_by_code(asset_error, 404)
                    or "not found" in str(asset_error).lower()
                ):
                    # Asset already deleted or doesn't exist, continue
                    logger.warning(
                        f"Warning: Asset {asset_uuid} not found during deletion",
                        extra={
                            "asset_id": str(asset_uuid),
                            "gumnut_id": str(gumnut_asset_id),
                            "error": str(asset_error),
                        },
                    )
                    continue
                else:
                    # For other errors, log but continue
                    logger.warning(
                        f"Warning: Failed to delete asset {asset_uuid}",
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
    return await _retrieve_and_stream_variant(id, client, "original")


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

    Streams the original video from CDN. Forwards Range headers for seeking.
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
