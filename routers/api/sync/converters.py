"""
Converter functions mapping Gumnut SDK types to Immich sync models.

Pure functions with no internal dependencies.
"""

import logging

from gumnut.types.album_response import AlbumResponse
from gumnut.types.asset_response import AssetResponse
from gumnut.types.exif_response import ExifResponse
from gumnut.types.face_response import FaceResponse
from gumnut.types.person_response import PersonResponse
from gumnut.types.user_response import UserResponse

from routers.immich_models import (
    AssetOrder,
    AssetVisibility,
    SyncAlbumV1,
    SyncAssetExifV1,
    SyncAssetFaceV1,
    SyncAssetV1,
    SyncAuthUserV1,
    SyncPersonV1,
    SyncUserV1,
)
from routers.utils.asset_conversion import mime_type_to_asset_type
from routers.utils.datetime_utils import (
    format_timezone_immich,
    to_actual_utc,
    to_immich_local_datetime,
)
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_album_id,
    safe_uuid_from_asset_id,
    safe_uuid_from_face_id,
    safe_uuid_from_person_id,
    safe_uuid_from_user_id,
)

logger = logging.getLogger(__name__)


def _format_exposure_time(exposure_time: float | None) -> str | None:
    """Format exposure time as a fraction string (e.g., '1/66')."""
    if exposure_time is None or exposure_time <= 0:
        return None
    if exposure_time >= 1:
        return str(exposure_time)
    denominator = round(1 / exposure_time)
    return f"1/{denominator}"


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
