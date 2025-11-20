"""
Immich sync endpoints for mobile client synchronization.

This module implements the Immich sync protocol, providing both streaming sync
(for beta timeline mode) and full/delta sync (for legacy timeline mode).
"""

import json
import logging
import uuid
from typing import AsyncGenerator, List
from datetime import timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from gumnut import Gumnut
from gumnut.types.asset_response import AssetResponse
from gumnut.types.person_response import PersonResponse
from gumnut.types.user_response import UserResponse

from routers.immich_models import (
    AssetDeltaSyncDto,
    AssetDeltaSyncResponseDto,
    AssetFullSyncDto,
    AssetResponseDto,
    AssetTypeEnum,
    AssetVisibility,
    SyncAckDeleteDto,
    SyncAckDto,
    SyncAckSetDto,
    SyncAssetExifV1,
    SyncAssetFaceV1,
    SyncAssetV1,
    SyncAuthUserV1,
    SyncPersonV1,
    SyncRequestType,
    SyncStreamDto,
    SyncUserV1,
    UserResponseDto,
)
from routers.utils.asset_conversion import (
    convert_gumnut_asset_to_immich,
    extract_exif_info,
)
from routers.utils.current_user import get_current_user
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_person_id,
    safe_uuid_from_user_id,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/sync",
    tags=["sync"],
    responses={404: {"description": "Not found"}},
)


@router.get("/ack")
async def get_sync_ack(
    http_request: Request,
) -> List[SyncAckDto]:
    """
    Get sync acknowledgements for the current user.

    Returns empty list - checkpoints are not stored (always full sync on interruption).
    """
    logger.info("GET /sync/ack called - returning empty (no checkpoint storage)")
    return []


@router.post("/ack", status_code=204)
async def send_sync_ack(
    request: SyncAckSetDto,
    http_request: Request,
):
    """
    Acknowledge sync checkpoints.

    Accepts acknowledgments but doesn't store them (no-op).
    """
    logger.info(
        f"Acknowledged {len(request.acks)} checkpoints (no-op)",
        extra={"checkpoint_count": len(request.acks)},
    )
    return


@router.delete("/ack", status_code=204)
async def delete_sync_ack(
    request: SyncAckDeleteDto,
    http_request: Request,
):
    """
    Delete sync acknowledgements - reset sync state.

    No-op since checkpoints are not stored.
    """
    logger.info("DELETE /sync/ack called (no-op - no checkpoints to clear)")
    return


@router.post("/delta-sync")
async def get_delta_sync(
    request: AssetDeltaSyncDto,
    gumnut_client: Gumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AssetDeltaSyncResponseDto:
    """
    Get delta sync data - returns assets modified after a specific timestamp.

    Note: This implementation fetches all assets and filters in-memory,
    which is inefficient for large libraries.
    """
    try:
        logger.info(f"Delta sync requested for timestamp: {request.updatedAfter}")

        upserted_assets = []
        page_size = 100
        starting_after_id = None

        # Paginate through all assets using cursor-based pagination
        while True:
            # Fetch a page of assets
            assets_page = gumnut_client.assets.list(
                limit=page_size,
                starting_after_id=starting_after_id,
            )

            # Convert to list to process the page
            page_assets = list(assets_page)
            if not page_assets:
                break

            # Filter and convert assets from this page
            for asset in page_assets:
                if asset.updated_at and asset.updated_at > request.updatedAfter:
                    asset_dto = convert_gumnut_asset_to_immich(asset, current_user)
                    upserted_assets.append(asset_dto)

            # Check if there are more pages
            if not assets_page.has_more:
                break

            # Update cursor for next page
            starting_after_id = page_assets[-1].id

        logger.info(
            f"Delta sync found {len(upserted_assets)} updated assets",
            extra={"asset_count": len(upserted_assets)},
        )

        # Note: Deletion tracking not supported by Gumnut
        deleted_asset_ids = []

        return AssetDeltaSyncResponseDto(
            deleted=deleted_asset_ids,
            needsFullSync=False,
            upserted=upserted_assets,
        )

    except Exception as e:
        logger.error(f"Error during delta sync: {str(e)}", exc_info=True)
        return AssetDeltaSyncResponseDto(
            deleted=[],
            needsFullSync=True,
            upserted=[],
        )


@router.post("/full-sync")
async def get_full_sync_for_user(
    request: AssetFullSyncDto,
    gumnut_client: Gumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> List[AssetResponseDto]:
    """
    Get paginated list of assets for full sync (legacy timeline mode).

    Supports cursor-based pagination using lastId parameter.
    """
    try:
        logger.info(
            "Full sync requested",
            extra={
                "limit": request.limit,
                "lastId": request.lastId,
                "updatedUntil": request.updatedUntil,
                "userId": request.userId,
            },
        )

        assets = []
        skip_until_cursor = request.lastId is not None

        for asset in gumnut_client.assets.list():
            # Skip until we find the cursor asset
            if skip_until_cursor:
                if asset.id == request.lastId:
                    skip_until_cursor = False
                    continue
                continue

            # Apply updatedUntil filter
            if request.updatedUntil and asset.updated_at:
                if asset.updated_at > request.updatedUntil:
                    continue

            asset_dto = convert_gumnut_asset_to_immich(asset, current_user)
            assets.append(asset_dto)

            if len(assets) >= request.limit:
                break

        logger.info(
            "Full sync completed",
            extra={
                "asset_count": len(assets),
                "limit": request.limit,
                "has_more": len(assets) >= request.limit,
            },
        )

        return assets

    except Exception as e:
        logger.error(f"Error during full sync: {str(e)}", exc_info=True)
        return []


def gumnut_user_to_sync_auth_user_v1(user: UserResponse) -> SyncAuthUserV1:
    """
    Convert Gumnut UserResponse to Immich SyncAuthUserV1 format.

    Args:
        user: Gumnut user data

    Returns:
        SyncAuthUserV1 for sync stream
    """
    # Construct full name from first and last name
    name_parts = []
    if user.first_name:
        name_parts.append(user.first_name)
    if user.last_name:
        name_parts.append(user.last_name)
    full_name = " ".join(name_parts) if name_parts else user.email or "Unknown User"

    return SyncAuthUserV1(
        id=str(safe_uuid_from_user_id(user.id)),
        email=user.email or "",
        name=full_name,
        hasProfileImage=False,
        profileChangedAt=user.updated_at,
        isAdmin=user.is_superuser,
        oauthId="",
        quotaUsageInBytes=0,
        avatarColor=None,
        deletedAt=None,
        pinCode=None,
        quotaSizeInBytes=None,
        storageLabel=None,
    )


def gumnut_user_to_sync_user_v1(user: UserResponse) -> SyncUserV1:
    """
    Convert Gumnut UserResponse to Immich SyncUserV1 format.

    Args:
        user: Gumnut user data

    Returns:
        SyncUserV1 for sync stream
    """
    # Construct full name from first and last name
    name_parts = []
    if user.first_name:
        name_parts.append(user.first_name)
    if user.last_name:
        name_parts.append(user.last_name)
    full_name = " ".join(name_parts) if name_parts else user.email or "Unknown User"

    return SyncUserV1(
        id=str(safe_uuid_from_user_id(user.id)),
        email=user.email or "",
        name=full_name,
        hasProfileImage=False,
        profileChangedAt=user.updated_at,
        avatarColor=None,
        deletedAt=None,
    )


def gumnut_asset_to_sync_asset_v1(asset: AssetResponse, owner_id: str) -> SyncAssetV1:
    """
    Convert Gumnut AssetResponse to Immich SyncAssetV1 format.

    Args:
        asset: Gumnut asset data
        owner_id: UUID of the asset owner

    Returns:
        SyncAssetV1 for sync stream
    """
    # Determine asset type from MIME type
    mime_type = asset.mime_type or "application/octet-stream"
    if mime_type.startswith("image/"):
        asset_type = AssetTypeEnum.IMAGE
    elif mime_type.startswith("video/"):
        asset_type = AssetTypeEnum.VIDEO
    elif mime_type.startswith("audio/"):
        asset_type = AssetTypeEnum.AUDIO
    else:
        asset_type = AssetTypeEnum.OTHER

    fileCreatedAt = asset.file_created_at
    fileModifiedAt = asset.file_modified_at
    localDateTime = asset.local_datetime

    if asset.exif:
        fileCreatedAt = asset.exif.original_datetime or fileCreatedAt
        fileModifiedAt = asset.exif.modified_datetime or fileModifiedAt
        localDateTime = asset.exif.original_datetime or localDateTime

    def ensure_tz_aware(dt):
        if dt and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    return SyncAssetV1(
        id=str(safe_uuid_from_asset_id(asset.id)),
        checksum=asset.checksum,
        isFavorite=False,  # Gumnut doesn't track favorites
        originalFileName=asset.original_file_name,
        ownerId=owner_id,
        type=asset_type,
        visibility=AssetVisibility.timeline,
        fileCreatedAt=ensure_tz_aware(fileCreatedAt),
        fileModifiedAt=ensure_tz_aware(fileModifiedAt),
        localDateTime=ensure_tz_aware(localDateTime),
        # Optional fields - use None when not available
        deletedAt=None,
        duration=None,
        libraryId=None,
        livePhotoVideoId=None,
        stackId=None,
        thumbhash=None,
    )


def gumnut_asset_to_sync_exif_v1(asset: AssetResponse) -> SyncAssetExifV1 | None:
    """
    Convert Gumnut AssetResponse EXIF data to Immich SyncAssetExifV1 format.

    Args:
        asset: Gumnut asset with EXIF data

    Returns:
        SyncAssetExifV1 if EXIF exists, None otherwise
    """
    if asset.exif is None:
        return None

    # Use existing EXIF extraction utility
    exif_dto = extract_exif_info(asset)

    return SyncAssetExifV1(
        assetId=str(safe_uuid_from_asset_id(asset.id)),
        city=exif_dto.city,
        country=exif_dto.country,
        dateTimeOriginal=exif_dto.dateTimeOriginal,
        description=exif_dto.description,
        exifImageHeight=int(exif_dto.exifImageHeight)
        if exif_dto.exifImageHeight
        else None,
        exifImageWidth=int(exif_dto.exifImageWidth)
        if exif_dto.exifImageWidth
        else None,
        exposureTime=exif_dto.exposureTime,
        fNumber=exif_dto.fNumber,
        fileSizeInByte=exif_dto.fileSizeInByte,
        focalLength=exif_dto.focalLength,
        fps=None,  # Not available in Gumnut
        iso=int(exif_dto.iso) if exif_dto.iso else None,
        latitude=exif_dto.latitude,
        lensModel=exif_dto.lensModel,
        longitude=exif_dto.longitude,
        make=exif_dto.make,
        model=exif_dto.model,
        modifyDate=exif_dto.modifyDate,
        orientation=exif_dto.orientation,
        profileDescription=None,  # Not available in Gumnut
        projectionType=exif_dto.projectionType,
        rating=int(exif_dto.rating) if exif_dto.rating else None,
        state=exif_dto.state,
        timeZone=exif_dto.timeZone,
    )


def gumnut_person_to_sync_person_v1(
    person: PersonResponse, owner_id: str
) -> SyncPersonV1:
    """
    Convert Gumnut PersonResponse to Immich SyncPersonV1 format.

    Args:
        person: Gumnut person data
        owner_id: UUID of the person owner

    Returns:
        SyncPersonV1 for sync stream
    """
    return SyncPersonV1(
        id=str(safe_uuid_from_person_id(person.id)),
        createdAt=person.created_at,
        isFavorite=False,
        isHidden=False,
        name=person.name or "",
        ownerId=owner_id,
        updatedAt=person.updated_at,
        birthDate=None,
        color=None,
        faceAssetId=None,
    )


def gumnut_asset_face_to_sync_v1(
    asset: AssetResponse, person: PersonResponse, person_index: int
) -> SyncAssetFaceV1:
    """
    Convert Gumnut asset person data to Immich SyncAssetFaceV1 format.

    Note: Gumnut doesn't provide bounding box data, so we use placeholders.

    Args:
        asset: Gumnut asset containing the face
        person: Gumnut person detected in the asset
        person_index: Index of this person in the asset's people list

    Returns:
        SyncAssetFaceV1 for sync stream
    """
    # Generate deterministic face ID from asset + person + index
    face_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{asset.id}-{person.id}-{person_index}",
        )
    )

    return SyncAssetFaceV1(
        id=face_id,
        assetId=str(safe_uuid_from_asset_id(asset.id)),
        boundingBoxX1=0,
        boundingBoxX2=0,
        boundingBoxY1=0,
        boundingBoxY2=0,
        imageHeight=asset.height or 0,
        imageWidth=asset.width or 0,
        sourceType="machine-learning",
        personId=str(safe_uuid_from_person_id(person.id)),
    )


async def generate_sync_stream(
    gumnut_client: Gumnut,
    request: SyncStreamDto,
) -> AsyncGenerator[str, None]:
    """
    Generate sync stream as JSON Lines (newline-delimited JSON).

    Streams various entity types requested by the client.
    Each line is a JSON object with: type, data, and ack (checkpoint ID).
    """
    try:
        # Get current user ONCE at the start (stateless - one call per stream)
        current_user = gumnut_client.users.me()
        owner_id = str(safe_uuid_from_user_id(current_user.id))

        requested_types = set(request.types)

        logger.info(
            f"Starting sync stream with {len(requested_types)} entity types",
            extra={"user_id": owner_id, "types": [t.value for t in requested_types]},
        )

        # Helper to generate checkpoint IDs
        def generate_checkpoint_id() -> str:
            return str(uuid.uuid4())

        # Stream auth users if requested (MUST be before assets for FK constraint)
        if SyncRequestType.AuthUsersV1 in requested_types:
            logger.info("Streaming auth users...", extra={"user_id": owner_id})

            sync_auth_user = gumnut_user_to_sync_auth_user_v1(current_user)
            event = {
                "type": SyncRequestType.AuthUsersV1.value,
                "data": sync_auth_user.model_dump(mode="json"),
                "ack": generate_checkpoint_id(),
            }
            yield json.dumps(event) + "\n"

            logger.info("Streamed 1 auth user", extra={"user_id": owner_id})

        # Stream users if requested (MUST be before assets for FK constraint)
        if SyncRequestType.UsersV1 in requested_types:
            logger.info("Streaming users...", extra={"user_id": owner_id})

            sync_user = gumnut_user_to_sync_user_v1(current_user)
            event = {
                "type": SyncRequestType.UsersV1.value,
                "data": sync_user.model_dump(mode="json"),
                "ack": generate_checkpoint_id(),
            }
            yield json.dumps(event) + "\n"

            logger.info("Streamed 1 user", extra={"user_id": owner_id})

        # Stream asset-related data in a single pass to avoid multiple iterations
        asset_related_types = {
            SyncRequestType.AssetsV1,
            SyncRequestType.AssetExifsV1,
            SyncRequestType.AssetFacesV1,
        }

        if any(t in requested_types for t in asset_related_types):
            logger.info("Streaming asset-related data...", extra={"user_id": owner_id})
            asset_count = 0
            exif_count = 0
            face_count = 0
            page_size = 100
            starting_after_id = None

            try:
                # Paginate through all assets using cursor-based pagination
                while True:
                    # Fetch a page of assets
                    assets_page = gumnut_client.assets.list(
                        limit=page_size,
                        starting_after_id=starting_after_id,
                    )

                    # Convert to list to process the page
                    page_assets = list(assets_page)
                    if not page_assets:
                        break

                    # Process each asset in the page
                    for asset in page_assets:
                        # Stream asset if requested
                        if SyncRequestType.AssetsV1 in requested_types:
                            sync_asset = gumnut_asset_to_sync_asset_v1(asset, owner_id)
                            event = {
                                "type": SyncRequestType.AssetsV1.value,
                                "data": sync_asset.model_dump(mode="json"),
                                "ack": generate_checkpoint_id(),
                            }
                            yield json.dumps(event) + "\n"
                            asset_count += 1

                        # Stream EXIF if requested and available
                        if SyncRequestType.AssetExifsV1 in requested_types:
                            sync_exif = gumnut_asset_to_sync_exif_v1(asset)
                            if sync_exif:
                                event = {
                                    "type": SyncRequestType.AssetExifsV1.value,
                                    "data": sync_exif.model_dump(mode="json"),
                                    "ack": generate_checkpoint_id(),
                                }
                                yield json.dumps(event) + "\n"
                                exif_count += 1

                        # Stream faces if requested and available
                        if (
                            SyncRequestType.AssetFacesV1 in requested_types
                            and asset.people
                        ):
                            for person_index, person in enumerate(asset.people):
                                sync_face = gumnut_asset_face_to_sync_v1(
                                    asset, person, person_index
                                )
                                event = {
                                    "type": SyncRequestType.AssetFacesV1.value,
                                    "data": sync_face.model_dump(mode="json"),
                                    "ack": generate_checkpoint_id(),
                                }
                                yield json.dumps(event) + "\n"
                                face_count += 1

                    # Check if there are more pages
                    if not assets_page.has_more:
                        break

                    # Update cursor for next page
                    starting_after_id = page_assets[-1].id

                    # Log progress after each page
                    logger.debug(
                        f"Progress: fetched page with {len(page_assets)} assets",
                        extra={
                            "user_id": owner_id,
                            "page_size": len(page_assets),
                            "total_assets": asset_count,
                        },
                    )

                # Log results for requested types
                if SyncRequestType.AssetsV1 in requested_types:
                    logger.info(
                        f"Streamed {asset_count} assets",
                        extra={"user_id": owner_id, "asset_count": asset_count},
                    )
                if SyncRequestType.AssetExifsV1 in requested_types:
                    logger.info(
                        f"Streamed {exif_count} EXIF records",
                        extra={"user_id": owner_id, "exif_count": exif_count},
                    )
                if SyncRequestType.AssetFacesV1 in requested_types:
                    logger.info(
                        f"Streamed {face_count} faces",
                        extra={"user_id": owner_id, "face_count": face_count},
                    )
            except Exception as e:
                logger.warning(f"Error streaming asset-related data: {str(e)}")

        # Stream people if requested
        if SyncRequestType.PeopleV1 in requested_types:
            try:
                logger.info("Streaming people...", extra={"user_id": owner_id})
                people_count = 0

                for person in gumnut_client.people.list():
                    sync_person = gumnut_person_to_sync_person_v1(person, owner_id)

                    event = {
                        "type": SyncRequestType.PeopleV1.value,
                        "data": sync_person.model_dump(mode="json"),
                        "ack": generate_checkpoint_id(),
                    }
                    yield json.dumps(event) + "\n"
                    people_count += 1

                logger.info(
                    f"Streamed {people_count} people",
                    extra={"user_id": owner_id, "people_count": people_count},
                )
            except Exception as e:
                logger.warning(f"Error streaming people: {str(e)}")

        # Stream completion event
        complete_event = {
            "type": "SyncCompleteV1",
            "data": {},
            "ack": generate_checkpoint_id(),
        }
        yield json.dumps(complete_event) + "\n"

        logger.info("Sync stream completed", extra={"user_id": owner_id})

    except Exception as e:
        logger.error(f"Error generating sync stream: {str(e)}", exc_info=True)
        error_event = {
            "type": "Error",
            "data": {"message": "Internal sync error occurred"},
            "ack": str(uuid.uuid4()),
        }
        yield json.dumps(error_event) + "\n"


@router.post("/stream")
async def get_sync_stream(
    request: SyncStreamDto,
    http_request: Request,
    gumnut_client: Gumnut = Depends(get_authenticated_gumnut_client),
):
    """
    Get sync stream as JSON Lines (application/jsonlines+json).

    Streams sync events for all requested entity types.
    """
    return StreamingResponse(
        generate_sync_stream(gumnut_client, request),
        media_type="application/jsonlines+json",
    )
