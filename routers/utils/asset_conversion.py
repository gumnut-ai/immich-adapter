"""
Utility functions for converting Gumnut assets to Immich format.

This module provides shared functionality for converting asset data from the Gumnut API
to the Immich API format, including metadata (camera/EXIF/GPS/location) processing.
"""

from datetime import datetime, timezone

from gumnut.types.asset_response import AssetResponse
from routers.utils.datetime_utils import (
    format_timezone_immich,
    to_actual_utc,
    to_immich_local_datetime,
)
from routers.immich_models import (
    AssetResponseDto,
    AssetTypeEnum,
    AssetVisibility,
    ExifResponseDto,
    SyncAssetExifV1,
    SyncAssetV1,
    UserResponseDto,
)
from services.websockets import AssetUploadReadyV1Payload
from routers.utils.gumnut_id_conversion import safe_uuid_from_asset_id
from routers.utils.person_conversion import convert_gumnut_person_to_immich_with_faces


def normalize_rating(rating: float | int | None) -> int | None:
    """Normalize a rating value: convert -1 (deprecated 'unrated') to None."""
    if rating is None:
        return None
    value = int(float(rating))
    return None if value == -1 else value


def mime_type_to_asset_type(mime_type: str) -> AssetTypeEnum:
    """
    Convert a MIME type string to an Immich AssetTypeEnum.

    Args:
        mime_type: The MIME type string (e.g., "image/jpeg", "video/mp4")

    Returns:
        AssetTypeEnum.IMAGE for image/* MIME types
        AssetTypeEnum.VIDEO for video/* MIME types
        AssetTypeEnum.AUDIO for audio/* MIME types
        AssetTypeEnum.OTHER for all other types
    """
    if mime_type.startswith("image/"):
        return AssetTypeEnum.IMAGE
    elif mime_type.startswith("video/"):
        return AssetTypeEnum.VIDEO
    elif mime_type.startswith("audio/"):
        return AssetTypeEnum.AUDIO
    else:
        return AssetTypeEnum.OTHER


def extract_exif_info(gumnut_asset: AssetResponse) -> ExifResponseDto:
    """
    Extract EXIF information from a Gumnut AssetResponse object.

    Args:
        gumnut_asset: The Gumnut AssetResponse object

    Returns:
        ExifResponseDto object with processed EXIF data
    """
    # Handle case where metadata might be None
    if gumnut_asset.metadata is None:
        return ExifResponseDto()

    metadata = gumnut_asset.metadata

    make = metadata.make
    model = metadata.model
    lens_model = metadata.lens_model
    f_number = metadata.f_number
    focal_length = metadata.focal_length
    iso = metadata.iso
    exposure_time = metadata.exposure_time
    latitude = metadata.latitude
    longitude = metadata.longitude
    city = metadata.city
    state = metadata.state
    country = metadata.country
    description = metadata.description
    orientation = metadata.orientation
    rating = metadata.rating
    projection_type = metadata.projection_type

    # convert exposure_time (float) to a fraction string like "1/66"
    if exposure_time is not None:
        if exposure_time >= 1:
            exposure_time_str = str(exposure_time)
        else:
            denominator = round(1 / exposure_time)
            exposure_time_str = f"1/{denominator}"
        exposure_time = exposure_time_str

    # Extract timezone before converting to UTC (need original offset for Immich format)
    time_zone = format_timezone_immich(metadata.original_datetime)

    # Convert datetimes to actual UTC for Immich compatibility
    date_time_original = to_actual_utc(metadata.original_datetime)
    modify_date = to_actual_utc(metadata.modified_datetime)

    width = gumnut_asset.width
    height = gumnut_asset.height
    file_size = gumnut_asset.file_size_bytes

    return ExifResponseDto(
        # Image dimensions
        exifImageWidth=int(float(width)) if width else None,
        exifImageHeight=int(float(height)) if height else None,
        # File info
        fileSizeInByte=int(file_size) if file_size else None,
        # Camera info
        make=str(make) if make else None,
        model=str(model) if model else None,
        lensModel=str(lens_model) if lens_model else None,
        # Camera settings
        fNumber=float(f_number) if f_number else None,
        focalLength=float(focal_length) if focal_length else None,
        iso=int(float(iso)) if iso else None,
        exposureTime=str(exposure_time) if exposure_time else None,
        # Location data
        latitude=float(latitude) if latitude else None,
        longitude=float(longitude) if longitude else None,
        city=str(city) if city else None,
        state=str(state) if state else None,
        country=str(country) if country else None,
        # Metadata
        description=str(description) if description else "",
        dateTimeOriginal=date_time_original,
        modifyDate=modify_date,
        orientation=str(orientation) if orientation else None,
        timeZone=time_zone,
        rating=normalize_rating(rating),
        projectionType=str(projection_type) if projection_type else None,
    )


def extract_sync_exif(gumnut_asset: AssetResponse, asset_uuid: str) -> SyncAssetExifV1:
    """
    Extract metadata from a Gumnut AssetResponse for sync events.

    Args:
        gumnut_asset: The Gumnut AssetResponse object
        asset_uuid: The asset UUID string

    Returns:
        SyncAssetExifV1 object with metadata from the asset
    """
    metadata = gumnut_asset.metadata

    # Extract metadata fields, defaulting to None if not available
    make = getattr(metadata, "make", None) if metadata else None
    model = getattr(metadata, "model", None) if metadata else None
    lens_model = getattr(metadata, "lens_model", None) if metadata else None
    f_number = getattr(metadata, "f_number", None) if metadata else None
    focal_length = getattr(metadata, "focal_length", None) if metadata else None
    iso = getattr(metadata, "iso", None) if metadata else None
    exposure_time = getattr(metadata, "exposure_time", None) if metadata else None
    latitude = getattr(metadata, "latitude", None) if metadata else None
    longitude = getattr(metadata, "longitude", None) if metadata else None
    city = getattr(metadata, "city", None) if metadata else None
    state = getattr(metadata, "state", None) if metadata else None
    country = getattr(metadata, "country", None) if metadata else None
    description = getattr(metadata, "description", None) if metadata else None
    orientation = getattr(metadata, "orientation", None) if metadata else None
    rating = getattr(metadata, "rating", None) if metadata else None
    projection_type = getattr(metadata, "projection_type", None) if metadata else None
    date_time_original = (
        getattr(metadata, "original_datetime", None) if metadata else None
    )
    modify_date = getattr(metadata, "modified_datetime", None) if metadata else None

    # Convert exposure_time (float) to a fraction string like "1/66"
    exposure_time_str = None
    if exposure_time is not None:
        if exposure_time >= 1:
            exposure_time_str = str(exposure_time)
        else:
            denominator = round(1 / exposure_time)
            exposure_time_str = f"1/{denominator}"

    # Extract timezone before converting to UTC (need original offset for Immich format)
    time_zone = format_timezone_immich(date_time_original)

    # Convert datetimes to actual UTC for Immich compatibility
    date_time_original = to_actual_utc(date_time_original)
    modify_date = to_actual_utc(modify_date)

    return SyncAssetExifV1(
        assetId=asset_uuid,
        city=str(city) if city else None,
        country=str(country) if country else None,
        dateTimeOriginal=date_time_original,
        description=str(description) if description else None,
        exifImageHeight=int(gumnut_asset.height) if gumnut_asset.height else None,
        exifImageWidth=int(gumnut_asset.width) if gumnut_asset.width else None,
        exposureTime=exposure_time_str,
        fNumber=float(f_number) if f_number else None,
        fileSizeInByte=int(gumnut_asset.file_size_bytes)
        if gumnut_asset.file_size_bytes
        else None,
        focalLength=float(focal_length) if focal_length else None,
        fps=None,  # Not available from Gumnut metadata
        iso=int(iso) if iso else None,
        latitude=float(latitude) if latitude else None,
        lensModel=str(lens_model) if lens_model else None,
        longitude=float(longitude) if longitude else None,
        make=str(make) if make else None,
        model=str(model) if model else None,
        modifyDate=modify_date,
        orientation=str(orientation) if orientation else None,
        profileDescription=None,  # Not available from Gumnut metadata
        projectionType=str(projection_type) if projection_type else None,
        rating=normalize_rating(rating),
        state=str(state) if state else None,
        timeZone=time_zone,
    )


def build_asset_upload_ready_payload(
    gumnut_asset: AssetResponse, owner_id: str
) -> AssetUploadReadyV1Payload:
    """
    Build an AssetUploadReadyV1Payload from a Gumnut asset for WebSocket sync events.

    Args:
        gumnut_asset: The Gumnut AssetResponse object
        owner_id: The owner's user ID string

    Returns:
        AssetUploadReadyV1Payload containing SyncAssetV1 and SyncAssetExifV1
    """
    asset_uuid = str(safe_uuid_from_asset_id(gumnut_asset.id))

    # Extract metadata datetimes for proper Immich compatibility
    metadata_original_dt = (
        gumnut_asset.metadata.original_datetime if gumnut_asset.metadata else None
    )
    metadata_modified_dt = (
        gumnut_asset.metadata.modified_datetime if gumnut_asset.metadata else None
    )

    # fileCreatedAt: capture time in actual UTC, fallback to upload time
    file_created_at = to_actual_utc(metadata_original_dt) or gumnut_asset.created_at
    # fileModifiedAt: modify time in actual UTC, fallback to upload time
    file_modified_at = to_actual_utc(metadata_modified_dt) or gumnut_asset.updated_at
    # localDateTime: capture time in keepLocalTime format, fallback to upload time
    local_date_time = (
        to_immich_local_datetime(metadata_original_dt) or gumnut_asset.created_at
    )

    sync_asset = SyncAssetV1(
        id=asset_uuid,
        ownerId=owner_id,
        thumbhash=None,
        checksum=gumnut_asset.checksum or "",
        deletedAt=None,
        duration=None,
        fileCreatedAt=file_created_at,
        fileModifiedAt=file_modified_at,
        height=int(gumnut_asset.height) if gumnut_asset.height else None,
        isEdited=False,
        isFavorite=False,
        libraryId=None,
        livePhotoVideoId=None,
        localDateTime=local_date_time,
        originalFileName=gumnut_asset.original_file_name or "",
        stackId=None,
        type=mime_type_to_asset_type(gumnut_asset.mime_type),
        visibility=AssetVisibility.timeline,
        width=int(gumnut_asset.width) if gumnut_asset.width else None,
    )

    sync_exif = extract_sync_exif(gumnut_asset, asset_uuid)

    return AssetUploadReadyV1Payload(asset=sync_asset, exif=sync_exif)


def convert_gumnut_asset_to_immich(
    gumnut_asset: AssetResponse, current_user: UserResponseDto
) -> AssetResponseDto:
    """
    Convert a Gumnut asset to AssetResponseDto format with comprehensive EXIF processing.

    Args:
        gumnut_asset: The Gumnut AssetResponse object
        current_user: The current user's UserResponseDto

    Returns:
        AssetResponseDto object with processed data and EXIF information
    """
    asset_id = gumnut_asset.id
    original_filename = gumnut_asset.original_file_name or "unknown"
    mime_type = gumnut_asset.mime_type or "application/octet-stream"
    checksum = gumnut_asset.checksum or ""

    # Extract metadata datetimes for proper Immich compatibility
    metadata_original_dt = (
        gumnut_asset.metadata.original_datetime if gumnut_asset.metadata else None
    )
    metadata_modified_dt = (
        gumnut_asset.metadata.modified_datetime if gumnut_asset.metadata else None
    )

    # Get fallback timestamps from upload times
    created_at_fallback = gumnut_asset.created_at or datetime.now(timezone.utc)
    updated_at_fallback = gumnut_asset.updated_at or datetime.now(timezone.utc)

    # fileCreatedAt: capture time in actual UTC, fallback to upload time
    file_created_at = to_actual_utc(metadata_original_dt) or created_at_fallback
    # fileModifiedAt: modify time in actual UTC, fallback to upload time
    file_modified_at = to_actual_utc(metadata_modified_dt) or updated_at_fallback
    # localDateTime: capture time in keepLocalTime format, fallback to upload time
    local_date_time = (
        to_immich_local_datetime(metadata_original_dt) or created_at_fallback
    )

    # Determine asset type based on MIME type
    asset_type = mime_type_to_asset_type(mime_type)

    people = []
    if gumnut_asset.people:
        for person in gumnut_asset.people:
            people.append(convert_gumnut_person_to_immich_with_faces(person))

    # Extract EXIF object directly from AssetResponse
    exif_info = extract_exif_info(gumnut_asset)

    return AssetResponseDto(
        id=str(safe_uuid_from_asset_id(asset_id)),
        deviceAssetId=str(asset_id),  # Keep original Gumnut asset ID
        deviceId="gumnut-device",  # Placeholder device ID
        type=asset_type,
        originalFileName=original_filename,
        originalMimeType=mime_type,
        fileCreatedAt=file_created_at,
        fileModifiedAt=file_modified_at,
        localDateTime=local_date_time,
        updatedAt=updated_at_fallback,
        checksum=checksum or "placeholder-checksum",
        exifInfo=exif_info,  # Now includes processed EXIF data
        createdAt=created_at_fallback,
        duration="00:00:00.000000" if asset_type == AssetTypeEnum.VIDEO else "",
        hasMetadata=True,
        height=float(gumnut_asset.height) if gumnut_asset.height else None,
        isArchived=False,
        isEdited=False,
        isFavorite=False,
        isOffline=False,
        isTrashed=False,
        originalPath=f"/gumnut/assets/{asset_id}",
        ownerId=current_user.id,
        owner=current_user,
        thumbhash="",
        visibility=AssetVisibility.timeline,
        width=float(gumnut_asset.width) if gumnut_asset.width else None,
        people=people,
    )
