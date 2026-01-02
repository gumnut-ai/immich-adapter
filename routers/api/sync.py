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
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
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

from services.checkpoint_store import (
    CheckpointStore,
    get_checkpoint_store,
)
from services.session_store import SessionStore, get_session_store

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
    safe_uuid_from_face_id,
    safe_uuid_from_person_id,
    safe_uuid_from_user_id,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/sync",
    tags=["sync"],
    responses={404: {"description": "Not found"}},
)


def _get_session_token(request: Request) -> UUID:
    """
    Extract and validate session token from request state.

    The auth middleware stores the session token in request.state.session_token.
    Sync endpoints require a session (API keys are not allowed).

    Args:
        request: The FastAPI request object

    Returns:
        The session UUID

    Raises:
        HTTPException: If session token is missing or invalid
    """
    session_token = getattr(request.state, "session_token", None)
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session required",
        )
    try:
        return UUID(session_token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid session token",
        )


def _parse_ack(ack: str) -> tuple[SyncEntityType, datetime | None] | None:
    """
    Parse an ack string into entity type and timestamp.

    Ack format for immich-adapter: "SyncEntityType|timestamp|"
    - SyncEntityType: Entity type string (e.g., "AssetV1", "AlbumV1")
    - timestamp: ISO 8601 datetime string
    - Trailing pipe for future additions

    Matches immich behavior: only throws for invalid entity types, skips
    malformed acks otherwise.

    Args:
        ack: The ack string to parse

    Returns:
        Tuple of (entity_type, last_synced_at), or None if ack is malformed

    Raises:
        HTTPException: If entity type is invalid (matches immich behavior)
    """
    parts = ack.split("|")
    if len(parts) < 2:
        logger.warning(f"Skipping malformed ack (too few parts): {ack}")
        return None

    entity_type_str = parts[0]
    timestamp_str = parts[1] if parts[1] else None

    # Validate entity type - immich throws BadRequestException for invalid types
    try:
        entity_type = SyncEntityType(entity_type_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ack type: {entity_type_str}",
        )

    # Parse timestamp if present - skip if malformed (don't throw)
    last_synced_at = None
    if timestamp_str:
        try:
            last_synced_at = datetime.fromisoformat(timestamp_str)
        except ValueError:
            logger.warning(f"Skipping ack with invalid timestamp: {ack}")
            return None

    return entity_type, last_synced_at


def _to_ack_string(entity_type: SyncEntityType, last_synced_at: datetime) -> str:
    """
    Convert entity type and timestamp to ack string.

    Ack format for immich-adapter: "SyncEntityType|timestamp|"

    Args:
        entity_type: The sync entity type
        last_synced_at: The timestamp from the last sync

    Returns:
        Formatted ack string
    """
    return f"{entity_type.value}|{last_synced_at.isoformat()}|"


@router.get("/ack")
async def get_sync_ack(
    http_request: Request,
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
) -> List[SyncAckDto]:
    """
    Get sync acknowledgements for the current session.

    Returns all stored checkpoints for the session, each containing:
    - type: The sync entity type (e.g., "AssetV1", "AlbumV1")
    - ack: The ack string in format "SyncEntityType|timestamp|"

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)

    checkpoints = await checkpoint_store.get_all(session_uuid)

    ack_dtos = [
        SyncAckDto(
            type=checkpoint.entity_type,
            ack=_to_ack_string(checkpoint.entity_type, checkpoint.last_synced_at),
        )
        for checkpoint in checkpoints
    ]

    logger.info(
        f"GET /sync/ack returning {len(ack_dtos)} checkpoints",
        extra={
            "session_id": str(session_uuid),
            "checkpoint_count": len(ack_dtos),
            "types": [dto.type.value for dto in ack_dtos],
        },
    )

    return ack_dtos


@router.post("/ack", status_code=204)
async def send_sync_ack(
    request: SyncAckSetDto,
    http_request: Request,
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
    session_store: SessionStore = Depends(get_session_store),
):
    """
    Acknowledge sync checkpoints.

    Parses each ack string and stores the checkpoint for the session.
    If any ack is for SyncResetV1, resets the session's sync progress
    (clears is_pending_sync_reset flag and deletes all checkpoints).

    Ack format for immich-adapter: "SyncEntityType|timestamp|"

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)
    session_token = str(session_uuid)

    # Parse all acks and collect checkpoints to store
    checkpoints_to_store: dict[SyncEntityType, datetime] = {}

    for ack in request.acks:
        parsed = _parse_ack(ack)
        if parsed is None:
            # Malformed ack - skip it (already logged)
            continue

        entity_type, last_synced_at = parsed

        # Handle SyncResetV1 specially - reset sync progress and return
        if entity_type == SyncEntityType.SyncResetV1:
            logger.info(
                "SyncResetV1 acknowledged - resetting sync progress",
                extra={"session_id": session_token},
            )
            # Clear the pending sync reset flag
            await session_store.set_pending_sync_reset(session_token, False)
            # Delete all existing checkpoints
            await checkpoint_store.delete_all(session_uuid)
            # Update session activity
            await session_store.update_activity(session_token)
            return

        # Store checkpoint (last one wins if duplicates)
        if last_synced_at:
            checkpoints_to_store[entity_type] = last_synced_at

    # Store all checkpoints atomically
    if checkpoints_to_store:
        await checkpoint_store.set_many(
            session_uuid,
            [
                (entity_type, timestamp)
                for entity_type, timestamp in checkpoints_to_store.items()
            ],
        )

    # Update session activity timestamp
    await session_store.update_activity(session_token)

    logger.info(
        f"Acknowledged {len(checkpoints_to_store)} checkpoints",
        extra={
            "session_id": session_token,
            "checkpoint_count": len(checkpoints_to_store),
            "types": [et.value for et in checkpoints_to_store.keys()],
        },
    )
    return


@router.delete("/ack", status_code=204)
async def delete_sync_ack(
    request: SyncAckDeleteDto,
    http_request: Request,
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
):
    """
    Delete sync acknowledgements - reset sync state.

    If types is None (not provided), deletes all checkpoints for the session.
    If types contains specific types, deletes only those checkpoint types.
    If types is an empty list, does nothing (no-op)

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)

    if request.types is None:
        # No types specified - delete all checkpoints
        await checkpoint_store.delete_all(session_uuid)
        logger.info(
            "Deleted all checkpoints",
            extra={"session_id": str(session_uuid)},
        )
    elif len(request.types) > 0:
        # Specific types requested - delete those
        await checkpoint_store.delete(session_uuid, request.types)
        logger.info(
            f"Deleted {len(request.types)} checkpoint types",
            extra={
                "session_id": str(session_uuid),
                "types": [t.value for t in request.types],
            },
        )
    # else: empty list - do nothing (no-op)
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


def _to_immich_local_datetime(dt: datetime | None) -> datetime | None:
    """
    Convert a datetime to Immich's "keepLocalTime" format.

    Immich stores localDateTime as a UTC timestamp that preserves local time values.
    For example, 10:00 AM PST becomes 10:00:00Z (not 18:00:00Z). This allows the
    mobile client to display the original local time regardless of viewer timezone.

    See immich/server/src/services/metadata.service.ts:870
    """
    if dt is None:
        return None
    # Strip timezone info, then mark as UTC to preserve the local time appearance
    return dt.replace(tzinfo=None).replace(tzinfo=timezone.utc)


def _to_actual_utc(dt: datetime | None) -> datetime | None:
    """
    Convert a datetime to actual UTC timestamp.

    If the datetime has timezone info, convert to UTC. If naive, assume UTC.
    This is used for fileCreatedAt which should be actual UTC (not keepLocalTime)
    so that the mobile client can correctly convert it to the user's local timezone.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Naive datetime - assume it's meant to be UTC
        return dt.replace(tzinfo=timezone.utc)
    # Convert timezone-aware datetime to UTC
    return dt.astimezone(timezone.utc)


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

    # fileCreatedAt: Use local_datetime (EXIF capture time) converted to actual UTC.
    # The mobile client applies SQLite's 'localtime' modifier to display in local time.
    # For a photo taken at 10:30 AM PST: fileCreatedAt = 18:30:00Z, mobile shows 10:30 AM.
    fileCreatedAt = _to_actual_utc(asset.local_datetime)
    fileModifiedAt = asset.file_modified_at
    # localDateTime: Use Immich's "keepLocalTime" format (local time values as UTC).
    # For a photo taken at 10:30 AM PST: localDateTime = 10:30:00Z (preserves local time).
    localDateTime = _to_immich_local_datetime(asset.local_datetime)

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


def gumnut_exif_to_sync_exif_v1(exif: ExifResponse) -> SyncAssetExifV1:
    """
    Convert Gumnut ExifResponse to Immich SyncAssetExifV1 format.

    Args:
        exif: Gumnut EXIF data

    Returns:
        SyncAssetExifV1 for sync stream
    """
    original_datetime = exif.original_datetime
    if original_datetime is not None and original_datetime.tzinfo is None:
        original_datetime = original_datetime.replace(tzinfo=timezone.utc)

    modified_datetime = exif.modified_datetime
    if modified_datetime is not None and modified_datetime.tzinfo is None:
        modified_datetime = modified_datetime.replace(tzinfo=timezone.utc)

    return SyncAssetExifV1(
        assetId=str(safe_uuid_from_asset_id(exif.asset_id)),
        city=exif.city,
        country=exif.country,
        dateTimeOriginal=original_datetime,
        description=exif.description,
        exifImageHeight=None,  # Not available in ExifResponse
        exifImageWidth=None,  # Not available in ExifResponse
        exposureTime=_format_exposure_time(exif.exposure_time),
        fNumber=exif.f_number,
        fileSizeInByte=None,  # Not available in ExifResponse
        focalLength=exif.focal_length,
        fps=exif.fps,
        iso=exif.iso,
        latitude=exif.latitude,
        lensModel=exif.lens_model,
        longitude=exif.longitude,
        make=exif.make,
        model=exif.model,
        modifyDate=modified_datetime,
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
    """Extract timezone in Immich's format (e.g., 'UTC+9', 'UTC-8', 'UTC+5:30').

    Immich stores timezone from exiftool which uses 'UTC+X' format without
    leading zeros. We need to match this format for consistency.

    Returns None if no timezone info, matching Immich's behavior.
    """
    if dt is None or dt.tzinfo is None:
        return None

    # Get the UTC offset as a timedelta
    offset = dt.utcoffset()
    if offset is None:
        return None

    # Calculate total seconds and convert to hours/minutes
    total_seconds = int(offset.total_seconds())
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes = remainder // 60

    # Build the timezone string in Immich's format
    sign = "+" if total_seconds >= 0 else "-"
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    else:
        return f"UTC{sign}{hours}"


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
        id=str(safe_uuid_from_face_id(face.id)),
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


def _convert_event_to_sync_entity(
    event: Data, owner_id: str
) -> tuple[SyncEntityType, dict, datetime] | None:
    """
    Convert a photos-api event to Immich sync entity.

    Args:
        event: Typed event from photos-api events endpoint
        owner_id: UUID of the owner

    Returns:
        Tuple of (SyncEntityType, data_dict, updated_at) or None if unsupported
    """
    if isinstance(event, AssetEventPayload):
        sync_model = gumnut_asset_to_sync_asset_v1(event.data, owner_id)
        return (
            SyncEntityType.AssetV1,
            sync_model.model_dump(mode="json"),
            event.data.updated_at,
        )

    elif isinstance(event, ExifEventPayload):
        sync_model = gumnut_exif_to_sync_exif_v1(event.data)
        return (
            SyncEntityType.AssetExifV1,
            sync_model.model_dump(mode="json"),
            event.data.updated_at,
        )

    elif isinstance(event, AlbumEventPayload):
        sync_model = gumnut_album_to_sync_album_v1(event.data, owner_id)
        return (
            SyncEntityType.AlbumV1,
            sync_model.model_dump(mode="json"),
            event.data.updated_at,
        )

    elif isinstance(event, AlbumAssetEventPayload):
        sync_model = gumnut_album_asset_to_sync_album_to_asset_v1(event.data)
        return (
            SyncEntityType.AlbumToAssetV1,
            sync_model.model_dump(mode="json"),
            event.data.updated_at,
        )

    elif isinstance(event, PersonEventPayload):
        sync_model = gumnut_person_to_sync_person_v1(event.data, owner_id)
        return (
            SyncEntityType.PersonV1,
            sync_model.model_dump(mode="json"),
            event.data.updated_at,
        )

    elif isinstance(event, FaceEventPayload):
        sync_model = gumnut_face_to_sync_face_v1(event.data)
        return (
            SyncEntityType.AssetFaceV1,
            sync_model.model_dump(mode="json"),
            event.data.updated_at,
        )

    return None


def _make_sync_event(
    entity_type: SyncEntityType,
    data: dict,
    updated_at: datetime,
) -> str:
    """
    Create a sync event JSON line.

    Args:
        entity_type: The Immich sync entity type
        data: The entity data dict
        updated_at: Timestamp for the checkpoint

    Returns:
        JSON line string with newline
    """
    # Ack format: SyncEntityType|timestamp| (trailing | for future additions)
    ack = f"{entity_type.value}|{updated_at.isoformat()}|"

    return (
        json.dumps(
            {
                "type": entity_type.value,
                "data": data,
                "ack": ack,
            }
        )
        + "\n"
    )


# Mapping from SyncRequestType to (gumnut_entity_type, SyncEntityType)
# Order matters - assets before exif, albums before album_assets, etc.
_SYNC_TYPE_ORDER: list[tuple[SyncRequestType, str, SyncEntityType]] = [
    (SyncRequestType.AssetsV1, "asset", SyncEntityType.AssetV1),
    (SyncRequestType.AlbumsV1, "album", SyncEntityType.AlbumV1),
    (SyncRequestType.AlbumToAssetsV1, "album_asset", SyncEntityType.AlbumToAssetV1),
    (SyncRequestType.AssetExifsV1, "exif", SyncEntityType.AssetExifV1),
    (SyncRequestType.PeopleV1, "person", SyncEntityType.PersonV1),
    (SyncRequestType.AssetFacesV1, "face", SyncEntityType.AssetFaceV1),
]


async def _stream_entity_type(
    gumnut_client: Gumnut,
    gumnut_entity_type: str,
    sync_entity_type: SyncEntityType,
    owner_id: str,
    checkpoint: datetime | None,
    sync_started_at: datetime,
) -> AsyncGenerator[tuple[str, int], None]:
    """
    Stream events for a single entity type.

    Args:
        gumnut_client: The Gumnut API client
        gumnut_entity_type: The entity type string for the Gumnut API (e.g., "asset")
        sync_entity_type: The Immich sync entity type (e.g., SyncEntityType.AssetV1)
        owner_id: The owner UUID string
        checkpoint: The last synced timestamp (None for full sync)
        sync_started_at: Upper bound for the query window

    Yields:
        Tuples of (json_line, count) for each event
    """
    last_updated_at = checkpoint
    count = 0

    while True:
        events_response = gumnut_client.events.get(
            updated_at_gte=last_updated_at,
            updated_at_lt=sync_started_at,
            entity_types=gumnut_entity_type,
            limit=500,
        )

        events = events_response.data
        if not events:
            break

        for event in events:
            result = _convert_event_to_sync_entity(event, owner_id)
            if result:
                entity_type, data, updated_at = result
                # Only yield if the entity type matches (safety check)
                if entity_type == sync_entity_type:
                    yield _make_sync_event(entity_type, data, updated_at), 1
                    count += 1

        # Get updated_at from last event for pagination cursor
        last_event = events[-1]
        last_updated_at = last_event.data.updated_at

        if len(events) < 500:
            break

    if count > 0:
        logger.debug(
            f"Streamed {count} {sync_entity_type.value} events",
            extra={"entity_type": sync_entity_type.value, "count": count},
        )


async def generate_sync_stream(
    gumnut_client: Gumnut,
    request: SyncStreamDto,
    checkpoint_map: dict[SyncEntityType, datetime],
) -> AsyncGenerator[str, None]:
    """
    Generate sync stream as JSON Lines (newline-delimited JSON).

    Uses the photos-api /api/events endpoint to fetch entity changes in
    priority order. Events are returned ordered by entity type priority
    (assets before exif, albums before album_assets, etc.), then by
    updated_at timestamp.

    Each entity type uses its own checkpoint for delta sync, only returning
    entities updated after the last acknowledged timestamp.

    Each line is a JSON object with: type, data, and ack (checkpoint ID).
    """
    try:
        # Get current user for owner_id
        current_user = gumnut_client.users.me()
        owner_id = str(safe_uuid_from_user_id(current_user.id))

        requested_types = set(request.types)

        logger.info(
            f"Starting sync stream with {len(requested_types)} entity types",
            extra={
                "user_id": owner_id,
                "types": [t.value for t in requested_types],
                "reset": request.reset,
                "checkpoints": len(checkpoint_map),
            },
        )

        # Stream auth user if requested (not in events endpoint)
        if SyncRequestType.AuthUsersV1 in requested_types:
            # Check checkpoint - only stream if user updated after checkpoint
            checkpoint = checkpoint_map.get(SyncEntityType.AuthUserV1)
            if checkpoint is None or current_user.updated_at > checkpoint:
                sync_auth_user = gumnut_user_to_sync_auth_user_v1(current_user)
                yield _make_sync_event(
                    SyncEntityType.AuthUserV1,
                    sync_auth_user.model_dump(mode="json"),
                    current_user.updated_at,
                )
                logger.debug("Streamed auth user", extra={"user_id": owner_id})

        # Stream user if requested (not in events endpoint)
        if SyncRequestType.UsersV1 in requested_types:
            # Check checkpoint - only stream if user updated after checkpoint
            checkpoint = checkpoint_map.get(SyncEntityType.UserV1)
            if checkpoint is None or current_user.updated_at > checkpoint:
                sync_user = gumnut_user_to_sync_user_v1(current_user)
                yield _make_sync_event(
                    SyncEntityType.UserV1,
                    sync_user.model_dump(mode="json"),
                    current_user.updated_at,
                )
                logger.debug("Streamed user", extra={"user_id": owner_id})

        # Capture sync start time to bound the query window
        sync_started_at = datetime.now(timezone.utc)

        # Counters for logging
        event_counts: dict[str, int] = {}
        total_events = 0

        # Process each entity type in order, using its own checkpoint
        for request_type, gumnut_entity_type, sync_entity_type in _SYNC_TYPE_ORDER:
            if request_type not in requested_types:
                continue

            # Get checkpoint for this entity type
            checkpoint = checkpoint_map.get(sync_entity_type)

            # Stream events for this entity type
            async for event_line, count in _stream_entity_type(
                gumnut_client,
                gumnut_entity_type,
                sync_entity_type,
                owner_id,
                checkpoint,
                sync_started_at,
            ):
                yield event_line
                event_counts[sync_entity_type.value] = (
                    event_counts.get(sync_entity_type.value, 0) + count
                )
                total_events += count

        # Log summary
        if total_events > 0:
            logger.info(
                "Streamed events from photos-api",
                extra={
                    "user_id": owner_id,
                    "total_events": total_events,
                    "event_counts": event_counts,
                },
            )

        # Stream completion event
        yield _make_sync_event(
            SyncEntityType.SyncCompleteV1, {}, datetime.now(timezone.utc)
        )
        logger.info("Sync stream completed", extra={"user_id": owner_id})

    except Exception as e:
        logger.error(f"Error generating sync stream: {str(e)}", exc_info=True)
        error_event = {
            "type": "Error",
            "data": {"message": "Internal sync error occurred"},
            "ack": str(uuid.uuid4()),
        }
        yield json.dumps(error_event) + "\n"


async def _generate_reset_stream() -> AsyncGenerator[str, None]:
    """
    Generate a sync stream containing only SyncResetV1.

    Used when the session has isPendingSyncReset flag set.
    Matches immich behavior: send SyncResetV1 and end immediately.
    """
    yield (
        json.dumps(
            {
                "type": SyncEntityType.SyncResetV1.value,
                "data": {},
                "ack": "SyncResetV1|reset",
            }
        )
        + "\n"
    )


@router.post("/stream")
async def get_sync_stream(
    request: SyncStreamDto,
    http_request: Request,
    gumnut_client: Gumnut = Depends(get_authenticated_gumnut_client),
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
    session_store: SessionStore = Depends(get_session_store),
):
    """
    Get sync stream as JSON Lines (application/jsonlines+json).

    Streams sync events for all requested entity types using the photos-api
    events endpoint. Events are returned in priority order to ensure proper
    entity dependencies (e.g., assets before exif data).

    Uses stored checkpoints to resume sync from last acknowledged position,
    only returning entities updated after the checkpoint timestamp.

    If request.reset is True, clears all checkpoints before streaming (full sync).
    If session has isPendingSyncReset flag, sends SyncResetV1 and ends immediately.
    """
    session_token = getattr(http_request.state, "session_token", None)
    session_uuid: UUID | None = None

    if session_token:
        try:
            session_uuid = UUID(session_token)
        except (ValueError, TypeError):
            # Invalid session token - continue without session features
            pass

    # Check if session has isPendingSyncReset flag set
    # If so, send SyncResetV1 and end immediately (matches immich behavior)
    if session_uuid:
        session = await session_store.get_by_id(str(session_uuid))
        if session and session.is_pending_sync_reset:
            logger.info(
                "Session has isPendingSyncReset flag - sending SyncResetV1",
                extra={"session_id": session_token},
            )
            return StreamingResponse(
                _generate_reset_stream(),
                media_type="application/jsonlines+json",
            )

    # Handle request.reset flag - clear all checkpoints before streaming
    # This triggers a full sync from the beginning
    if request.reset and session_uuid:
        logger.info(
            "request.reset=True - clearing all checkpoints for full sync",
            extra={"session_id": session_token},
        )
        await checkpoint_store.delete_all(session_uuid)

    # Load checkpoints for delta sync (empty dict if no session or no checkpoints)
    checkpoint_map: dict[SyncEntityType, datetime] = {}
    if session_uuid and not request.reset:
        checkpoints = await checkpoint_store.get_all(session_uuid)
        checkpoint_map = {cp.entity_type: cp.last_synced_at for cp in checkpoints}
        logger.debug(
            f"Loaded {len(checkpoint_map)} checkpoints for sync stream",
            extra={
                "session_id": session_token,
                "checkpoint_types": [t.value for t in checkpoint_map.keys()],
            },
        )

    return StreamingResponse(
        generate_sync_stream(gumnut_client, request, checkpoint_map),
        media_type="application/jsonlines+json",
    )
