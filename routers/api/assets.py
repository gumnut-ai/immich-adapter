from typing import List
from uuid import UUID
import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Header,
    UploadFile,
    File,
    Form,
    Query,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from gumnut import Gumnut

from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.error_mapping import map_gumnut_error, check_for_error_by_code
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
    CheckExistingAssetsResponseDto,
    UpdateAssetDto,
)
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_asset_id,
)
from routers.utils.asset_conversion import convert_gumnut_asset_to_immich

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/assets",
    tags=["assets"],
    responses={404: {"description": "Not found"}},
)


async def _download_asset_content(
    asset_uuid: UUID, client: Gumnut, size: AssetMediaSize = AssetMediaSize.fullsize
) -> Response:
    """
    Shared helper function to download asset content from Gumnut.

    Args:
        asset_uuid: The asset UUID to download
        client: Authenticated Gumnut client
        size: The size variant to download (fullsize, preview, thumbnail)

    Returns:
        FastAPI Response with the asset content
    """

    try:
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)

        # Helper function to create streaming generator
        def create_streaming_generator(streaming_response_context):
            with streaming_response_context as gumnut_response:
                for chunk in gumnut_response.iter_bytes(chunk_size=8192):
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

        def create_streaming_response(streaming_context_factory):
            """Create a StreamingResponse with proper header extraction."""
            # Get headers first
            with streaming_context_factory() as gumnut_response:
                content_type, response_headers = extract_headers_and_filename(
                    gumnut_response
                )

            # Create new streaming context for actual streaming
            return StreamingResponse(
                create_streaming_generator(streaming_context_factory()),
                media_type=content_type,
                headers=response_headers,
            )

        # Determine the size and use appropriate Gumnut SDK function with streaming
        if size == AssetMediaSize.fullsize:
            # Use streaming download for original size
            def streaming_factory():
                return client.assets.with_streaming_response.download(gumnut_asset_id)

            return create_streaming_response(streaming_factory)
        else:
            # Use streaming download_thumbnail for thumbnail and preview sizes
            # Map Immich size to Gumnut size parameter
            if size == AssetMediaSize.preview:
                gumnut_size = "preview"
            else:  # default to thumbnail
                gumnut_size = "thumbnail"

            def streaming_factory():
                return client.assets.with_streaming_response.download_thumbnail(
                    gumnut_asset_id, size=gumnut_size
                )

            return create_streaming_response(streaming_factory)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch asset")


@router.post("/bulk-upload-check")
async def bulk_upload_check(
    request: AssetBulkUploadCheckDto,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> AssetBulkUploadCheckResponseDto:
    """
    Check which assets from a bulk upload already exist in Gumnut. This is done via a checksum, which Gumnut does not
    support, so all uploads are accepted.
    """
    results = []

    for asset in request.assets:
        results.append(
            {
                "id": asset.id,
                "action": "accept",  # Gumnut SDK does not have the right API for an existence check, so always return "accept"
            }
        )

    return AssetBulkUploadCheckResponseDto(results=results)


@router.post("/exist")
async def check_existing_assets(
    request: CheckExistingAssetsDto,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> CheckExistingAssetsResponseDto:
    """
    Check if multiple assets exist on the server and return all existing.
    This is a stub implementation as Gumnut does not support checking asset existence by device asset IDs.
    Returns an empty list (no existing assets found).
    """
    # Stub implementation: return empty list since Gumnut doesn't support existence checking
    return CheckExistingAssetsResponseDto(existingIds=[])


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
    x_immich_checksum: str = Header(default=None, alias="x-immich-checksum"),
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> AssetMediaResponseDto:
    """
    Upload an asset using the Gumnut SDK.
    Creates a new asset in Gumnut from the provided asset data.
    """

    try:
        from datetime import datetime

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
        from routers.utils.gumnut_id_conversion import safe_uuid_from_asset_id

        asset_uuid = safe_uuid_from_asset_id(asset_id)

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

            except Exception as asset_error:
                # Log individual asset errors but continue with other deletions
                if (
                    check_for_error_by_code(asset_error, 404)
                    or "not found" in str(asset_error).lower()
                ):
                    # Asset already deleted or doesn't exist, continue
                    logger.warning(
                        f"Warning: Asset {asset_uuid} not found during deletion"
                    )
                    continue
                else:
                    # For other errors, log but continue
                    logger.warning(
                        f"Warning: Failed to delete asset {asset_uuid}: {asset_error}"
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
            mime_type = asset.mime_type or ""
            if mime_type.startswith("image/"):
                image_count += 1
            elif mime_type.startswith("video/"):
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
) -> AssetResponseDto:
    """
    Update asset metadata.
    This is a stub implementation as Gumnut does not support asset metadata updates.
    Returns the asset as-is.
    """
    try:
        gumnut_asset_id = uuid_to_gumnut_asset_id(id)
        gumnut_asset = client.assets.retrieve(gumnut_asset_id)
        immich_asset = convert_gumnut_asset_to_immich(gumnut_asset)
        return immich_asset
    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch asset")


@router.get("/{id}")
async def get_asset_info(
    id: UUID,
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> AssetResponseDto:
    try:
        gumnut_asset_id = uuid_to_gumnut_asset_id(id)

        # Retrieve the specific asset from Gumnut
        gumnut_asset = client.assets.retrieve(gumnut_asset_id)

        # Convert Gumnut asset to AssetResponseDto format
        immich_asset = convert_gumnut_asset_to_immich(gumnut_asset)

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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Download the original asset file.
    Always downloads the full-size original asset using the shared download logic.
    """
    return await _download_asset_content(id, client, AssetMediaSize.fullsize)


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
    Downloads and streams the fullsize video asset.
    """
    return await _download_asset_content(id, client, AssetMediaSize.fullsize)
