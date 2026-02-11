"""
Immich sync endpoints for mobile client synchronization.

This module implements the Immich sync protocol, providing both streaming sync
(for beta timeline mode) and full/delta sync (for legacy timeline mode).

The streaming sync uses the photos-api v2 events endpoint (/api/v2/events) to
fetch lightweight event records, then batch-fetches full entities as needed.
Events are processed in priority order (assets before exif, etc.).
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, List, TypeAlias, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from gumnut import Gumnut

from gumnut.types.album_response import AlbumResponse
from gumnut.types.asset_response import AssetResponse
from gumnut.types.events_v2_response import Data as EventV2Data
from gumnut.types.exif_response import ExifResponse
from gumnut.types.face_response import FaceResponse
from gumnut.types.person_response import PersonResponse
from gumnut.types.user_response import UserResponse

from services.checkpoint_store import (
    Checkpoint,
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
    AssetVisibility,
    SyncAckDeleteDto,
    SyncAckDto,
    SyncAckSetDto,
    SyncAlbumDeleteV1,
    SyncAlbumV1,
    SyncAssetDeleteV1,
    SyncAssetExifV1,
    SyncAssetFaceDeleteV1,
    SyncAssetFaceV1,
    SyncAssetV1,
    SyncAuthUserV1,
    SyncEntityType,
    SyncPersonDeleteV1,
    SyncPersonV1,
    SyncRequestType,
    SyncStreamDto,
    SyncUserV1,
    UserResponseDto,
)
from routers.utils.asset_conversion import (
    convert_gumnut_asset_to_immich,
    mime_type_to_asset_type,
)
from routers.utils.current_user import get_current_user
from routers.utils.datetime_utils import (
    format_timezone_immich,
    to_actual_utc,
    to_immich_local_datetime,
)
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


def _parse_ack(ack: str) -> tuple[SyncEntityType, str] | None:
    """
    Parse an ack string into entity type and cursor.

    Ack format for immich-adapter: "SyncEntityType|cursor|"
    - SyncEntityType: Entity type string (e.g., "AssetV1", "AlbumV1")
    - cursor: Opaque v2 events cursor
    - Trailing pipe for future additions

    Matches immich behavior: only throws for invalid entity types, skips
    malformed acks otherwise.

    Args:
        ack: The ack string to parse

    Returns:
        Tuple of (entity_type, cursor), or None if ack is malformed.

    Raises:
        HTTPException: If entity type is invalid (matches immich behavior)
    """
    parts = ack.split("|")
    if len(parts) < 2:
        logger.warning(
            "Skipping malformed ack (too few parts)",
            extra={"ack": ack},
        )
        return None

    entity_type_str = parts[0]

    # Validate entity type - immich throws BadRequestException for invalid types
    try:
        entity_type = SyncEntityType(entity_type_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ack type: {entity_type_str}",
        )

    cursor = parts[1] if parts[1] else ""

    return entity_type, cursor


def _to_ack_string(
    entity_type: SyncEntityType,
    cursor: str,
) -> str:
    """
    Convert entity type and cursor to ack string.

    Ack format for immich-adapter: "SyncEntityType|cursor|"

    Args:
        entity_type: The sync entity type
        cursor: The opaque v2 events cursor

    Returns:
        Formatted ack string
    """
    return f"{entity_type.value}|{cursor}|"


@router.get("/ack")
async def get_sync_ack(
    http_request: Request,
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
) -> List[SyncAckDto]:
    """
    Get sync acknowledgements for the current session.

    Returns all stored checkpoints for the session, each containing:
    - type: The sync entity type (e.g., "AssetV1", "AlbumV1")
    - ack: The ack string in format "SyncEntityType|cursor|"

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)

    checkpoints = await checkpoint_store.get_all(session_uuid)

    ack_dtos = [
        SyncAckDto(
            type=checkpoint.entity_type,
            ack=_to_ack_string(
                checkpoint.entity_type,
                checkpoint.cursor or "",
            ),
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

    Ack format for immich-adapter: "SyncEntityType|cursor|"

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)
    session_token = str(session_uuid)

    # Parse all acks and collect checkpoints to store
    # Value is cursor string
    checkpoints_to_store: dict[SyncEntityType, str] = {}

    for idx, ack in enumerate(request.acks):
        parsed = _parse_ack(ack)
        if parsed is None:
            # Malformed ack - skip it (already logged)
            continue

        entity_type, cursor = parsed

        # Handle SyncResetV1 specially - reset sync progress and return
        if entity_type == SyncEntityType.SyncResetV1:
            # Warn if there are other acks that will be ignored
            remaining_acks = len(request.acks) - idx - 1
            ignored_count = len(checkpoints_to_store) + remaining_acks
            if ignored_count > 0:
                logger.warning(
                    "SyncResetV1 encountered - ignoring other acks",
                    extra={
                        "session_id": session_token,
                        "ignored_checkpoint_count": len(checkpoints_to_store),
                        "ignored_remaining_count": remaining_acks,
                    },
                )
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
        checkpoints_to_store[entity_type] = cursor

    # Store all checkpoints atomically
    if checkpoints_to_store:
        await checkpoint_store.set_many(
            session_uuid,
            [
                (entity_type, cursor)
                for entity_type, cursor in checkpoints_to_store.items()
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
    asset_type = mime_type_to_asset_type(asset.mime_type)

    # fileCreatedAt: Use local_datetime (EXIF capture time) converted to actual UTC.
    # The mobile client applies SQLite's 'localtime' modifier to display in local time.
    # For a photo taken at 10:30 AM PST: fileCreatedAt = 18:30:00Z, mobile shows 10:30 AM.
    fileCreatedAt = to_actual_utc(asset.local_datetime)
    fileModifiedAt = asset.file_modified_at
    # localDateTime: Use Immich's "keepLocalTime" format (local time values as UTC).
    # For a photo taken at 10:30 AM PST: localDateTime = 10:30:00Z (preserves local time).
    localDateTime = to_immich_local_datetime(asset.local_datetime)

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
    # Convert EXIF datetimes to actual UTC for Immich compatibility
    original_datetime = to_actual_utc(exif.original_datetime)
    modified_datetime = to_actual_utc(exif.modified_datetime)

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
        timeZone=format_timezone_immich(exif.original_datetime),
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


def _format_exposure_time(exposure_time: float | None) -> str | None:
    """Format exposure time as a fraction string (e.g., '1/66')."""
    if exposure_time is None or exposure_time <= 0:
        return None
    if exposure_time >= 1:
        return str(exposure_time)
    denominator = round(1 / exposure_time)
    return f"1/{denominator}"


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


def _make_sync_event(
    entity_type: SyncEntityType,
    data: dict,
    cursor: str,
) -> str:
    """
    Create a sync event JSON line.

    Args:
        entity_type: The Immich sync entity type
        data: The entity data dict
        cursor: The opaque v2 events cursor for checkpointing

    Returns:
        JSON line string with newline
    """
    ack = _to_ack_string(entity_type, cursor)

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
    # album_asset skipped — no bulk fetch endpoint yet (GUM-254)
    (SyncRequestType.AssetExifsV1, "exif", SyncEntityType.AssetExifV1),
    (SyncRequestType.PeopleV1, "person", SyncEntityType.PersonV1),
    (SyncRequestType.AssetFacesV1, "face", SyncEntityType.AssetFaceV1),
]

# Page size for events API pagination
EVENTS_PAGE_SIZE = 500

# Batch size for entity fetch API calls (conservative to avoid upstream limits)
FETCH_BATCH_SIZE = 100

# Delete event types that are converted to Immich delete sync models
_DELETE_EVENT_TYPES = frozenset(
    {
        "asset_deleted",
        "album_deleted",
        "person_deleted",
        "face_deleted",
    }
)

# Event types that are intentionally skipped (not converted to sync events)
_SKIPPED_EVENT_TYPES = frozenset(
    {
        "exif_deleted",  # Immich handles via asset deletion
        "album_asset_removed",  # No bulk fetch endpoint yet (GUM-254)
    }
)

# Expected entity_type for each delete event_type
_DELETE_EVENT_ENTITY_TYPES: dict[str, str] = {
    "asset_deleted": "asset",
    "album_deleted": "album",
    "person_deleted": "person",
    "face_deleted": "face",
}


_EntityType: TypeAlias = (
    AssetResponse | AlbumResponse | PersonResponse | FaceResponse | ExifResponse
)


def _batched(items: list[str], size: int) -> list[list[str]]:
    """Split a list into chunks of the given size."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _fetch_entities_map(
    gumnut_client: Gumnut,
    gumnut_entity_type: str,
    entity_ids: list[str],
) -> dict[str, _EntityType]:
    """
    Batch-fetch entities by ID and return a dict keyed by entity ID.

    IDs are chunked into batches of FETCH_BATCH_SIZE to avoid exceeding
    upstream API limits. Missing entities (deleted between event and fetch)
    result in fewer entries.

    Args:
        gumnut_client: The Gumnut API client
        gumnut_entity_type: The entity type string (e.g., "asset", "album")
        entity_ids: List of entity IDs to fetch

    Returns:
        Dict mapping entity_id -> entity object
    """
    if not entity_ids:
        return {}

    unique_ids = list(dict.fromkeys(entity_ids))  # Deduplicate, preserve order
    result: dict[str, _EntityType] = {}

    for chunk in _batched(unique_ids, FETCH_BATCH_SIZE):
        if gumnut_entity_type == "asset":
            page = gumnut_client.assets.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page})

        elif gumnut_entity_type == "album":
            page = gumnut_client.albums.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page})

        elif gumnut_entity_type == "person":
            page = gumnut_client.people.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page})

        elif gumnut_entity_type == "face":
            page = gumnut_client.faces.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page})

        elif gumnut_entity_type == "exif":
            # Exif is 1:1 with asset; v2 exif events use entity_id = asset_id
            page = gumnut_client.assets.list(ids=chunk, limit=len(chunk))
            for asset in page:
                if asset.exif:
                    result[asset.exif.asset_id] = asset.exif

        else:
            logger.warning(
                "Unknown entity type in _fetch_entities_map",
                extra={"gumnut_entity_type": gumnut_entity_type},
            )
            return {}

    return result


def _make_delete_sync_event(
    event: EventV2Data,
) -> tuple[str, SyncEntityType] | None:
    """
    Convert a v2 delete event to an Immich delete sync event JSON line.

    Validates that event.entity_type matches expectations for the delete
    event_type. Logs a warning and skips if there's a mismatch.

    Args:
        event: The v2 event with a delete event_type

    Returns:
        Tuple of (json_line, sync_entity_type) or None if event should be skipped
    """
    # Validate entity_type matches the delete event_type
    expected_entity_type = _DELETE_EVENT_ENTITY_TYPES.get(event.event_type)
    if expected_entity_type and event.entity_type != expected_entity_type:
        logger.warning(
            "Delete event entity_type mismatch, skipping",
            extra={
                "event_type": event.event_type,
                "entity_type": event.entity_type,
                "expected_entity_type": expected_entity_type,
                "entity_id": event.entity_id,
            },
        )
        return None

    if event.event_type == "asset_deleted":
        data = SyncAssetDeleteV1(assetId=str(safe_uuid_from_asset_id(event.entity_id)))
        return (
            _make_sync_event(
                SyncEntityType.AssetDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.AssetDeleteV1,
        )

    elif event.event_type == "album_deleted":
        data = SyncAlbumDeleteV1(albumId=str(safe_uuid_from_album_id(event.entity_id)))
        return (
            _make_sync_event(
                SyncEntityType.AlbumDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.AlbumDeleteV1,
        )

    elif event.event_type == "person_deleted":
        data = SyncPersonDeleteV1(
            personId=str(safe_uuid_from_person_id(event.entity_id))
        )
        return (
            _make_sync_event(
                SyncEntityType.PersonDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.PersonDeleteV1,
        )

    elif event.event_type == "face_deleted":
        data = SyncAssetFaceDeleteV1(
            assetFaceId=str(safe_uuid_from_face_id(event.entity_id))
        )
        return (
            _make_sync_event(
                SyncEntityType.AssetFaceDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.AssetFaceDeleteV1,
        )

    logger.warning(
        "Unhandled delete event type in _make_delete_sync_event",
        extra={
            "event_type": event.event_type,
            "entity_id": event.entity_id,
        },
    )
    return None


def _convert_entity_to_sync_event(
    gumnut_entity_type: str,
    entity: AssetResponse
    | AlbumResponse
    | PersonResponse
    | FaceResponse
    | ExifResponse,
    owner_id: str,
    cursor: str,
    sync_entity_type: SyncEntityType,
) -> str:
    """
    Convert a fetched entity to an Immich sync event JSON line.

    Args:
        gumnut_entity_type: The entity type string (e.g., "asset", "album")
        entity: The fetched entity object
        owner_id: UUID of the owner
        cursor: The v2 event cursor for the ack string
        sync_entity_type: The Immich sync entity type

    Returns:
        JSON line string with newline
    """
    if gumnut_entity_type == "asset":
        sync_model = gumnut_asset_to_sync_asset_v1(
            cast(AssetResponse, entity), owner_id
        )
    elif gumnut_entity_type == "album":
        sync_model = gumnut_album_to_sync_album_v1(
            cast(AlbumResponse, entity), owner_id
        )
    elif gumnut_entity_type == "person":
        sync_model = gumnut_person_to_sync_person_v1(
            cast(PersonResponse, entity), owner_id
        )
    elif gumnut_entity_type == "face":
        sync_model = gumnut_face_to_sync_face_v1(cast(FaceResponse, entity))
    elif gumnut_entity_type == "exif":
        sync_model = gumnut_exif_to_sync_exif_v1(cast(ExifResponse, entity))
    else:
        raise ValueError(f"Unsupported entity type: {gumnut_entity_type}")

    return _make_sync_event(
        sync_entity_type,
        sync_model.model_dump(mode="json"),
        cursor,
    )


async def _stream_entity_type(
    gumnut_client: Gumnut,
    gumnut_entity_type: str,
    sync_entity_type: SyncEntityType,
    owner_id: str,
    checkpoint: Checkpoint | None,
    sync_started_at: datetime,
) -> AsyncGenerator[tuple[str, int], None]:
    """
    Stream events for a single entity type using the v2 events API.

    Fetches lightweight v2 events, then batch-fetches full entities for
    upsert events. Delete events are converted directly to Immich delete
    sync events.

    Args:
        gumnut_client: The Gumnut API client
        gumnut_entity_type: The entity type string for the Gumnut API (e.g., "asset")
        sync_entity_type: The Immich sync entity type (e.g., SyncEntityType.AssetV1)
        owner_id: The owner UUID string
        checkpoint: The checkpoint with cursor (None for full sync)
        sync_started_at: Upper bound for the query window

    Yields:
        Tuples of (json_line, count) for each event
    """
    last_cursor = checkpoint.cursor if checkpoint else None
    count = 0

    while True:
        # Build params for v2 events API
        params: dict[str, Any] = {
            "created_at_lt": sync_started_at,
            "entity_types": gumnut_entity_type,
            "limit": EVENTS_PAGE_SIZE,
        }
        if last_cursor is not None:
            params["after_cursor"] = last_cursor

        events_response = gumnut_client.events_v2.get(**params)

        events = events_response.data
        if not events:
            break

        # Collect entity IDs from upsert events (non-delete, non-skipped)
        upsert_ids = [
            event.entity_id
            for event in events
            if event.event_type not in _DELETE_EVENT_TYPES
            and event.event_type not in _SKIPPED_EVENT_TYPES
        ]

        # Batch-fetch entities for upserts
        entities_map = _fetch_entities_map(
            gumnut_client, gumnut_entity_type, upsert_ids
        )

        # Process events in order
        for event in events:
            if event.event_type in _SKIPPED_EVENT_TYPES:
                # Intentionally skipped event type — advance cursor only
                logger.debug(
                    "Skipping unsupported event type",
                    extra={
                        "event_type": event.event_type,
                        "entity_id": event.entity_id,
                        "entity_type": event.entity_type,
                    },
                )
                continue

            if event.event_type in _DELETE_EVENT_TYPES:
                # Delete event — convert directly
                result = _make_delete_sync_event(event)
                if result:
                    json_line, _ = result
                    yield json_line, 1
                    count += 1
            else:
                # Upsert event — look up fetched entity
                entity = entities_map.get(event.entity_id)
                if entity is None:
                    # Entity was deleted between event and fetch — skip
                    continue
                json_line = _convert_entity_to_sync_event(
                    gumnut_entity_type, entity, owner_id, event.cursor, sync_entity_type
                )
                yield json_line, 1
                count += 1

        # Update cursor from last event
        last_cursor = events[-1].cursor

        if not events_response.has_more:
            break

    if count > 0:
        logger.debug(
            f"Streamed {count} {sync_entity_type.value} events",
            extra={"entity_type": sync_entity_type.value, "count": count},
        )


async def generate_sync_stream(
    gumnut_client: Gumnut,
    request: SyncStreamDto,
    checkpoint_map: dict[SyncEntityType, Checkpoint],
) -> AsyncGenerator[str, None]:
    """
    Generate sync stream as JSON Lines (newline-delimited JSON).

    Uses the photos-api v2 events endpoint to fetch lightweight event records
    in priority order, then batch-fetches full entities for upsert events.

    Each entity type uses its own checkpoint with an opaque cursor for
    cursor-based pagination.

    Each line is a JSON object with: type, data, and ack (checkpoint cursor).
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

        # Stream auth user if requested (not from events API)
        if SyncRequestType.AuthUsersV1 in requested_types:
            # Check checkpoint — only stream if no checkpoint exists
            # (user entities don't go through v2 events, always stream on full sync)
            checkpoint = checkpoint_map.get(SyncEntityType.AuthUserV1)
            if checkpoint is None:
                sync_auth_user = gumnut_user_to_sync_auth_user_v1(current_user)
                yield _make_sync_event(
                    SyncEntityType.AuthUserV1,
                    sync_auth_user.model_dump(mode="json"),
                    current_user.id,
                )
                logger.debug("Streamed auth user", extra={"user_id": owner_id})

        # Stream user if requested (not from events API)
        if SyncRequestType.UsersV1 in requested_types:
            # Check checkpoint — only stream if no checkpoint exists
            checkpoint = checkpoint_map.get(SyncEntityType.UserV1)
            if checkpoint is None:
                sync_user = gumnut_user_to_sync_user_v1(current_user)
                yield _make_sync_event(
                    SyncEntityType.UserV1,
                    sync_user.model_dump(mode="json"),
                    current_user.id,
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
        yield _make_sync_event(SyncEntityType.SyncCompleteV1, {}, "")
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
    checkpoint_map: dict[SyncEntityType, Checkpoint] = {}
    if session_uuid and not request.reset:
        checkpoints = await checkpoint_store.get_all(session_uuid)
        checkpoint_map = {cp.entity_type: cp for cp in checkpoints}
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
