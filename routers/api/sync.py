"""
Immich sync endpoints for mobile client synchronization.

This module implements the Immich sync protocol, providing both streaming sync
(for beta timeline mode) and full/delta sync (for legacy timeline mode).

The streaming sync uses the photos-api /api/events endpoint to fetch entity
changes in priority order, ensuring proper entity dependencies (assets before
exif, albums before album_assets, etc.).
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from gumnut import Gumnut
from gumnut.types.album_asset_event_payload import AlbumAssetEventPayload
from gumnut.types.album_asset_response import AlbumAssetResponse
from gumnut.types.album_event_payload import AlbumEventPayload
from gumnut.types.album_response import AlbumResponse
from gumnut.types.asset_event_payload import AssetEventPayload
from gumnut.types.asset_response import AssetResponse
from gumnut.types.events_response import Data
from gumnut.types.exif_event_payload import ExifEventPayload
from gumnut.types.exif_response import ExifResponse
from gumnut.types.face_event_payload import FaceEventPayload
from gumnut.types.face_response import FaceResponse
from gumnut.types.person_event_payload import PersonEventPayload
from gumnut.types.person_response import PersonResponse
from gumnut.types.user_response import UserResponse

from routers.immich_models import (
    AssetDeltaSyncDto,
    AssetDeltaSyncResponseDto,
    AssetFullSyncDto,
    AssetOrder,
    AssetResponseDto,
    AssetTypeEnum,
    AssetVisibility,
    SyncAckDeleteDto,
    SyncAckDto,
    SyncAckSetDto,
    SyncAlbumToAssetV1,
    SyncAlbumV1,
    SyncAssetExifV1,
    SyncAssetFaceV1,
    SyncAssetV1,
    SyncAuthUserV1,
    SyncEntityType,
    SyncPersonV1,
    SyncRequestType,
    SyncStreamDto,
    SyncUserV1,
    UserResponseDto,
)
from routers.utils.asset_conversion import convert_gumnut_asset_to_immich
from routers.utils.current_user import get_current_user
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_album_id,
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

    if asset.checksum_sha1 is None:
        logger.warning(
            f"Asset {asset.id} has no checksum_sha1, using checksum instead",
            extra={"asset_id": asset.id, "checksum": asset.checksum},
        )

    return SyncAssetV1(
        id=str(safe_uuid_from_asset_id(asset.id)),
        checksum=asset.checksum_sha1 or asset.checksum,
        isFavorite=False,  # Gumnut doesn't track favorites
        originalFileName=asset.original_file_name,
        ownerId=owner_id,
        type=asset_type,
        visibility=AssetVisibility.timeline,
        fileCreatedAt=fileCreatedAt,
        fileModifiedAt=fileModifiedAt,
        localDateTime=localDateTime,
        # Optional fields - use None when not available
        deletedAt=None,
        duration=None,
        libraryId=None,
        livePhotoVideoId=None,
        stackId=None,
        thumbhash=None,
    )


def gumnut_exif_to_sync_exif_v1(
    exif: ExifResponse,
    *,
    height: int | None = None,
    width: int | None = None,
    file_size_bytes: int | None = None,
) -> SyncAssetExifV1:
    """
    Convert Gumnut ExifResponse to Immich SyncAssetExifV1 format.

    Args:
        exif: Gumnut EXIF data
        height: Optional asset height (not available in EXIF events)
        width: Optional asset width (not available in EXIF events)
        file_size_bytes: Optional file size (not available in EXIF events)

    Returns:
        SyncAssetExifV1 for sync stream
    """
    return SyncAssetExifV1(
        assetId=str(safe_uuid_from_asset_id(exif.asset_id)),
        city=exif.city,
        country=exif.country,
        dateTimeOriginal=exif.original_datetime,
        description=exif.description,
        exifImageHeight=height,
        exifImageWidth=width,
        exposureTime=_format_exposure_time(exif.exposure_time),
        fNumber=exif.f_number,
        fileSizeInByte=file_size_bytes,
        focalLength=exif.focal_length,
        fps=exif.fps,
        iso=exif.iso,
        latitude=exif.latitude,
        lensModel=exif.lens_model,
        longitude=exif.longitude,
        make=exif.make,
        model=exif.model,
        modifyDate=exif.modified_datetime,
        orientation=str(exif.orientation) if exif.orientation is not None else None,
        profileDescription=exif.profile_description,
        projectionType=exif.projection_type,
        rating=exif.rating,
        state=exif.state,
        timeZone=_extract_timezone(exif.original_datetime),
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
        isFavorite=person.is_favorite,
        isHidden=person.is_hidden,
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


def _format_exposure_time(exposure_time: float | None) -> str | None:
    """Format exposure time as a fraction string (e.g., '1/66')."""
    if exposure_time is None or exposure_time <= 0:
        return None
    if exposure_time >= 1:
        return str(exposure_time)
    denominator = round(1 / exposure_time)
    return f"1/{denominator}"


def _extract_timezone(dt: datetime | None) -> str | None:
    """Extract timezone name from datetime."""
    if dt is None:
        return None
    tz_name = dt.tzname()
    return tz_name if tz_name else None


def gumnut_album_to_sync_album_v1(album: AlbumResponse, owner_id: str) -> SyncAlbumV1:
    """Convert Gumnut AlbumResponse to Immich SyncAlbumV1 format."""
    thumbnail_asset_id = None
    if album.album_cover_asset_id:
        thumbnail_asset_id = str(safe_uuid_from_asset_id(album.album_cover_asset_id))

    return SyncAlbumV1(
        id=str(safe_uuid_from_album_id(album.id)),
        ownerId=owner_id,
        name=album.name,
        description=album.description or "",
        createdAt=album.created_at,
        updatedAt=album.updated_at,
        thumbnailAssetId=thumbnail_asset_id,
        isActivityEnabled=True,
        order=AssetOrder.desc,
    )


def gumnut_album_asset_to_sync_album_to_asset_v1(
    album_asset: AlbumAssetResponse,
) -> SyncAlbumToAssetV1:
    """Convert Gumnut AlbumAssetResponse to Immich SyncAlbumToAssetV1 format."""
    return SyncAlbumToAssetV1(
        albumId=str(safe_uuid_from_album_id(album_asset.album_id)),
        assetId=str(safe_uuid_from_asset_id(album_asset.asset_id)),
    )


def gumnut_face_to_sync_face_v1(face: FaceResponse) -> SyncAssetFaceV1:
    """Convert Gumnut FaceResponse to Immich SyncAssetFaceV1 format."""
    bounding_box = face.bounding_box

    person_id = None
    if face.person_id:
        person_id = str(safe_uuid_from_person_id(face.person_id))

    return SyncAssetFaceV1(
        id=face.id,
        assetId=str(safe_uuid_from_asset_id(face.asset_id)),
        boundingBoxX1=bounding_box.get("x", 0),
        boundingBoxX2=bounding_box.get("x", 0) + bounding_box.get("w", 0),
        boundingBoxY1=bounding_box.get("y", 0),
        boundingBoxY2=bounding_box.get("y", 0) + bounding_box.get("h", 0),
        imageHeight=0,
        imageWidth=0,
        sourceType="machine-learning",
        personId=person_id,
    )


def _map_request_types_to_entity_types(
    requested_types: set[SyncRequestType],
) -> list[str]:
    """
    Map Immich SyncRequestTypes to photos-api entity_types.

    Args:
        requested_types: Set of Immich sync request types

    Returns:
        List of photos-api entity type strings
    """
    entity_types = []

    if SyncRequestType.AssetsV1 in requested_types:
        entity_types.append("asset")
    if SyncRequestType.AssetExifsV1 in requested_types:
        entity_types.append("exif")
    if SyncRequestType.AlbumsV1 in requested_types:
        entity_types.append("album")
    if SyncRequestType.AlbumToAssetsV1 in requested_types:
        entity_types.append("album_asset")
    if SyncRequestType.PeopleV1 in requested_types:
        entity_types.append("person")
    if SyncRequestType.AssetFacesV1 in requested_types:
        entity_types.append("face")

    return entity_types


def _convert_event_to_sync_entity(
    event: Data, owner_id: str
) -> tuple[SyncEntityType, dict] | None:
    """
    Convert a photos-api event to Immich sync entity.

    Args:
        event: Typed event from photos-api events endpoint
        owner_id: UUID of the owner

    Returns:
        Tuple of (SyncEntityType, data_dict) or None if unsupported entity type
    """
    if isinstance(event, AssetEventPayload):
        sync_model = gumnut_asset_to_sync_asset_v1(event.data, owner_id)
        return (SyncEntityType.AssetV1, sync_model.model_dump(mode="json"))

    elif isinstance(event, ExifEventPayload):
        sync_model = gumnut_exif_to_sync_exif_v1(event.data)
        return (SyncEntityType.AssetExifV1, sync_model.model_dump(mode="json"))

    elif isinstance(event, AlbumEventPayload):
        sync_model = gumnut_album_to_sync_album_v1(event.data, owner_id)
        return (SyncEntityType.AlbumV1, sync_model.model_dump(mode="json"))

    elif isinstance(event, AlbumAssetEventPayload):
        sync_model = gumnut_album_asset_to_sync_album_to_asset_v1(event.data)
        return (SyncEntityType.AlbumToAssetV1, sync_model.model_dump(mode="json"))

    elif isinstance(event, PersonEventPayload):
        sync_model = gumnut_person_to_sync_person_v1(event.data, owner_id)
        return (SyncEntityType.PersonV1, sync_model.model_dump(mode="json"))

    elif isinstance(event, FaceEventPayload):
        sync_model = gumnut_face_to_sync_face_v1(event.data)
        return (SyncEntityType.AssetFaceV1, sync_model.model_dump(mode="json"))

    return None


def _make_sync_event(entity_type: SyncEntityType, data: dict) -> str:
    """
    Create a sync event JSON line.

    Args:
        entity_type: The Immich sync entity type
        data: The entity data dict

    Returns:
        JSON line string with newline
    """
    return (
        json.dumps(
            {
                "type": entity_type.value,
                "data": data,
                "ack": f"{entity_type.value}|{uuid.uuid4()}",
            }
        )
        + "\n"
    )


async def generate_sync_stream(
    gumnut_client: Gumnut,
    request: SyncStreamDto,
) -> AsyncGenerator[str, None]:
    """
    Generate sync stream as JSON Lines (newline-delimited JSON).

    Uses the photos-api /api/events endpoint to fetch entity changes in
    priority order. Events are returned ordered by entity type priority
    (assets before exif, albums before album_assets, etc.), then by
    updated_at timestamp.

    Each line is a JSON object with: type, data, and ack (checkpoint ID).
    """
    try:
        # Get current user for owner_id
        current_user = gumnut_client.users.me()
        owner_id = str(safe_uuid_from_user_id(current_user.id))

        requested_types = set(request.types)

        logger.info(
            "Starting sync stream",
            extra={
                "user_id": owner_id,
                "types": [t.value for t in requested_types],
                "reset": request.reset,
            },
        )

        # Stream auth user if requested (not in events endpoint)
        if SyncRequestType.AuthUsersV1 in requested_types:
            sync_auth_user = gumnut_user_to_sync_auth_user_v1(current_user)
            yield _make_sync_event(
                SyncEntityType.AuthUserV1,
                sync_auth_user.model_dump(mode="json"),
            )
            logger.debug("Streamed auth user", extra={"user_id": owner_id})

        # Stream user if requested (not in events endpoint)
        if SyncRequestType.UsersV1 in requested_types:
            sync_user = gumnut_user_to_sync_user_v1(current_user)
            yield _make_sync_event(
                SyncEntityType.UserV1,
                sync_user.model_dump(mode="json"),
            )
            logger.debug("Streamed user", extra={"user_id": owner_id})

        # Map requested types to photos-api entity_types
        entity_types = _map_request_types_to_entity_types(requested_types)

        if entity_types:
            # Capture sync start time to bound the query window
            sync_started_at = datetime.now(timezone.utc)

            # Counters for logging
            event_counts: dict[str, int] = {}
            total_events = 0

            # Paginate through events using time-based cursor
            last_updated_at: datetime | None = None

            while True:
                # Fetch page of events from photos-api
                events_response = gumnut_client.events.get(
                    updated_at_gte=last_updated_at,
                    updated_at_lt=sync_started_at,
                    entity_types=",".join(entity_types),
                    limit=500,
                )

                # Access the data attribute (list of events)
                events = events_response.data
                if not events:
                    break

                # Stream each event
                for event in events:
                    result = _convert_event_to_sync_entity(event, owner_id)
                    if result:
                        entity_type, data = result
                        yield _make_sync_event(entity_type, data)

                        # Track counts
                        entity_type_str = entity_type.value
                        event_counts[entity_type_str] = (
                            event_counts.get(entity_type_str, 0) + 1
                        )
                        total_events += 1

                # Get updated_at from last event for pagination cursor
                last_event = events[-1]
                last_updated_at = last_event.data.updated_at

                # Check if this was the last page
                if len(events) < 500:
                    break

                logger.debug(
                    "Fetched events page",
                    extra={
                        "user_id": owner_id,
                        "page_size": len(events),
                        "total_events": total_events,
                    },
                )

            # Log summary
            logger.info(
                "Streamed events from photos-api",
                extra={
                    "user_id": owner_id,
                    "total_events": total_events,
                    "event_counts": event_counts,
                },
            )

        # Stream completion event
        yield _make_sync_event(SyncEntityType.SyncCompleteV1, {})
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

    Streams sync events for all requested entity types using the photos-api
    events endpoint. Events are returned in priority order to ensure proper
    entity dependencies (e.g., assets before exif data).
    """
    return StreamingResponse(
        generate_sync_stream(gumnut_client, request),
        media_type="application/jsonlines+json",
    )
