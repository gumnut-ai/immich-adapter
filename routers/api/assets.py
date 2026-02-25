from typing import List
from uuid import UUID, uuid4
import base64
import logging
from datetime import datetime

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Form,
    Query,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from gumnut import AsyncGumnut, Gumnut, GumnutError

from routers.utils.gumnut_client import (
    get_authenticated_async_gumnut_client,
    get_authenticated_gumnut_client,
)
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
    AssetJobsDto,
    AssetMediaReplaceDto,
    AssetMediaSize,
    AssetMediaResponseDto,
    AssetMediaStatus,
    AssetMetadataKey,
    AssetMetadataResponseDto,
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


async def _download_asset_content(
    asset_uuid: UUID,
    client: AsyncGumnut,
    size: AssetMediaSize = AssetMediaSize.fullsize,
    force_original: bool = False,
) -> Response:
    """
    Shared helper function to download asset content from Gumnut.

    Uses the async Gumnut client with async streaming to avoid blocking the
    event loop during downloads. This is critical for health check responsiveness
    under load — see GUM-289.

    Args:
        asset_uuid: The asset UUID to download
        client: Authenticated async Gumnut client
        size: The size variant to download (fullsize, preview, thumbnail)
        force_original: If True, always download the original file via download().
                       If False, use download_thumbnail() for all sizes.

    Returns:
        FastAPI Response with the asset content

    Note on HEIC handling:
        The /thumbnail endpoint should always use download_thumbnail(), even for
        fullsize requests. This returns browser-compatible formats (WEBP) for
        non-web-native formats like HEIC. See GUM-223 for details.

        The /original endpoint should always use download() to return the actual
        original file regardless of format.
    """

    try:
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)

        # Async streaming generator that yields chunks without blocking the event loop
        async def create_async_streaming_generator(streaming_response_context):
            async with streaming_response_context as gumnut_response:
                async for chunk in gumnut_response.aiter_bytes(chunk_size=8192):
                    yield chunk

        def extract_headers_and_filename(gumnut_response):
            """Extract content type, filename, and build response headers."""
            content_type = gumnut_response.headers.get(
                "content-type", "application/octet-stream"
            )

            # Extract filename from content-disposition header if available
            content_disposition = gumnut_response.headers.get("content-disposition", "")
            filename = None
            if 'filename="' in content_disposition:
                filename = content_disposition.split('filename="')[1].split('"')[0]

            # Build response headers
            response_headers = {"Content-Type": content_type}
            if filename:
                response_headers["Content-Disposition"] = (
                    f'inline; filename="{filename}"'
                )

            return content_type, response_headers

        async def create_async_streaming_response(streaming_context_factory):
            """Create a StreamingResponse with proper header extraction."""
            # Get headers first
            async with streaming_context_factory() as gumnut_response:
                content_type, response_headers = extract_headers_and_filename(
                    gumnut_response
                )

            # Create new streaming context for actual streaming
            return StreamingResponse(
                create_async_streaming_generator(streaming_context_factory()),
                media_type=content_type,
                headers=response_headers,
            )

        # For /original endpoint: always return the actual original file
        # This preserves the original format (JPEG, HEIC, etc.) for download
        if force_original:

            def streaming_factory():
                return client.assets.with_streaming_response.download(gumnut_asset_id)

            return await create_async_streaming_response(streaming_factory)

        # For /thumbnail endpoint: use download_thumbnail for ALL sizes
        # This ensures browser-compatible formats are served for non-web-native
        # formats like HEIC (photos-api converts HEIC to WEBP for fullsize thumbnails)
        size_map = {
            AssetMediaSize.fullsize: "fullsize",
            AssetMediaSize.preview: "preview",
            AssetMediaSize.thumbnail: "thumbnail",
        }
        gumnut_size = size_map.get(size, "thumbnail")

        def streaming_factory():
            return client.assets.with_streaming_response.download_thumbnail(
                gumnut_asset_id, size=gumnut_size
            )

        return await create_async_streaming_response(streaming_factory)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch asset")


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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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

        existing_assets_response = client.assets.check_existence(
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
        raise map_gumnut_error(e, "Failed to check bulk upload assets")


@router.post("/exist")
async def check_existing_assets(
    request: CheckExistingAssetsDto,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> CheckExistingAssetsResponseDto:
    """
    Check if multiple assets exist on the server and return all existing.
    """
    try:
        existing_assets_response = client.assets.check_existence(
            device_id=request.deviceId, device_asset_ids=request.deviceAssetIds
        )
        existing_ids = [
            str(safe_uuid_from_asset_id(asset.id))
            for asset in existing_assets_response.assets
        ]
    except Exception as e:
        raise map_gumnut_error(e, "Failed to check existing assets")

    return CheckExistingAssetsResponseDto(existingIds=existing_ids)


@router.post(
    "",
    status_code=201,
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {"$ref": "#/components/schemas/AssetMediaCreateDto"}
                }
            }
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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AssetMediaResponseDto:
    """
    Upload an asset using the Gumnut SDK.
    Creates a new asset in Gumnut from the provided asset data.
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

        # Read the binary data from the uploaded file
        asset_data = await assetData.read()

        # Drop iOS live photo .MOV files — they upload as separate video files
        # that would become orphan assets since Gumnut doesn't support live photos.
        # The Immich mobile client sends .MOV files with content_type
        # "application/octet-stream", so we check both the content type and the
        # file extension to determine if this might be a video.
        filename_lower = (assetData.filename or "").lower()
        may_be_video = (
            assetData.content_type and assetData.content_type.startswith("video/")
        ) or filename_lower.endswith((".mov", ".mp4", ".m4v"))
        if may_be_video and is_live_photo_video(asset_data):
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

        # Create asset using Gumnut SDK
        gumnut_asset = client.assets.create(
            asset_data=(assetData.filename, asset_data, assetData.content_type),
            device_asset_id=deviceAssetId,
            device_id=deviceId,
            file_created_at=file_created_at,
            file_modified_at=file_modified_at,
        )

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
        # Handle specific upload error cases first
        error_msg = str(e).lower()
        if "duplicate" in error_msg or "already exists" in error_msg:
            # If it's a duplicate, we still need an asset ID
            # This is a simplified approach - in a real implementation you'd extract the existing asset ID
            return AssetMediaResponseDto(
                id="00000000-0000-0000-0000-000000000000",  # Placeholder
                status=AssetMediaStatus.duplicate,
            )
        elif check_for_error_by_code(e, 413) or "too large" in error_msg:
            raise HTTPException(status_code=413, detail="Asset file too large")
        elif check_for_error_by_code(e, 415) or "unsupported" in error_msg:
            raise HTTPException(status_code=415, detail="Unsupported media type")
        else:
            # Use the general error mapper for other cases
            raise map_gumnut_error(e, "Failed to upload asset")


@router.put("", status_code=204)
async def update_assets(
    request: AssetBulkUpdateDto,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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

                client.assets.delete(gumnut_asset_id)

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
        raise map_gumnut_error(e, "Failed to delete assets")


@router.get("/device/{deviceId}")
async def get_all_user_assets_by_device_id(
    deviceId: str,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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

        for asset in gumnut_assets:
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
        raise map_gumnut_error(e, "Failed to fetch asset statistics")


@router.get("/random")
async def get_random(
    count: int = Query(default=None, ge=1, type="number"),
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Run asset jobs.
    This is a stub implementation as Gumnut does not support running asset jobs.
    Returns HTTP 204 (No Content) as specified by the Immich API.
    """
    # Stub implementation: asset jobs are not supported in Gumnut
    return Response(status_code=204)


@router.put("/{id}")
async def update_asset(
    id: UUID,
    request: UpdateAssetDto,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AssetResponseDto:
    try:
        gumnut_asset_id = uuid_to_gumnut_asset_id(id)

        # Retrieve the specific asset from Gumnut
        gumnut_asset = client.assets.retrieve(gumnut_asset_id)

        # Convert Gumnut asset to AssetResponseDto format
        immich_asset = convert_gumnut_asset_to_immich(gumnut_asset, current_user)

        return immich_asset

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch asset")


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
    client: AsyncGumnut = Depends(get_authenticated_async_gumnut_client),
) -> Response:
    """
    Get a thumbnail for an asset.
    Uses the shared download logic with size defaulting to thumbnail if not specified.
    """
    # Determine the size, defaulting to thumbnail if not specified
    preferred_size = size if size is not None else AssetMediaSize.thumbnail
    return await _download_asset_content(id, client, preferred_size)


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
    client: AsyncGumnut = Depends(get_authenticated_async_gumnut_client),
) -> Response:
    """
    Download the original asset file.

    Always downloads the original file using download(), preserving the original
    format (JPEG, HEIC, RAW, etc.) for actual downloads.

    Note: force_original=True ensures we call download() instead of download_thumbnail().
    This is important for preserving the original format when users explicitly
    request the original file (e.g., for editing or archival purposes).
    """
    return await _download_asset_content(
        id, client, AssetMediaSize.fullsize, force_original=True
    )


@router.put(
    "/{id}/original",
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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
):
    """
    Replace the asset with new file, without changing its id.
    Deprecated in immich and not supported by Gumnut.
    """
    return


@router.get("/{id}/metadata")
async def get_asset_metadata(
    id: UUID,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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
    key: AssetMetadataKey,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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
    key: AssetMetadataKey,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
):
    """
    Play the video for a specific asset.
    This is a stub implementation as Gumnut does not support video playback.
    Returns HTTP 200 (OK) as specified by the Immich API.
    """
    return Response(status_code=status.HTTP_200_OK)
