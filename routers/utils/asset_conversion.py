"""
Utility functions for converting Gumnut assets to Immich format.

This module provides shared functionality for converting asset data from the Gumnut API
to the Immich API format, including EXIF data processing.
"""

from datetime import datetime, timezone

from gumnut.types.asset_response import AssetResponse
from routers.immich_models import (
    AssetResponseDto,
    AssetTypeEnum,
    AssetVisibility,
    ExifResponseDto,
    UserResponseDto,
)
from routers.utils.gumnut_id_conversion import safe_uuid_from_asset_id
from routers.utils.person_conversion import convert_gumnut_person_to_immich_with_faces


def extract_exif_info(gumnut_asset: AssetResponse) -> ExifResponseDto:
    """
    Extract EXIF information from a Gumnut AssetResponse object.

    Args:
        gumnut_asset: The Gumnut AssetResponse object

    Returns:
        ExifResponseDto object with processed EXIF data
    """
    # Handle case where exif might be None
    if gumnut_asset.exif is None:
        return ExifResponseDto()

    exif = gumnut_asset.exif

    # Extract EXIF fields directly from Gumnut Exif object
    # Note: These fields may not all be present in the Gumnut Exif type, using direct access where available
    make = exif.make
    model = exif.model
    lens_model = exif.lens_model
    f_number = exif.f_number
    focal_length = exif.focal_length
    iso = exif.iso
    exposure_time = exif.exposure_time
    latitude = exif.latitude
    longitude = exif.longitude
    city = exif.city
    state = exif.state
    country = exif.country
    description = exif.description
    orientation = exif.orientation
    rating = exif.rating
    projection_type = exif.projection_type

    # convert exposure_time (float) to a fraction string like "1/66"
    if exposure_time is not None:
        if exposure_time >= 1:
            exposure_time_str = str(exposure_time)
        else:
            denominator = round(1 / exposure_time)
            exposure_time_str = f"1/{denominator}"
        exposure_time = exposure_time_str

    # Map Gumnut datetime fields to our expected names
    date_time_original = exif.original_datetime
    modify_date = exif.modified_datetime

    width = gumnut_asset.width
    height = gumnut_asset.height
    file_size = gumnut_asset.file_size_bytes

    time_zone = None

    # Pydantic will throw an error if date_time_original does not have a timezone
    if date_time_original is not None:
        time_zone = date_time_original.tzname()
        if not time_zone:
            time_zone = "Etc/UTC"
            date_time_original = date_time_original.replace(tzinfo=timezone.utc)

    # Handle timezone for modify_date as well
    if modify_date is not None and modify_date.tzname() is None:
        modify_date = modify_date.replace(tzinfo=timezone.utc)

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
        timeZone=str(time_zone),
        rating=int(float(rating)) if rating else None,
        projectionType=str(projection_type) if projection_type else None,
    )


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
    file_created_at = gumnut_asset.created_at
    file_modified_at = gumnut_asset.updated_at
    checksum = gumnut_asset.checksum or ""

    # Ensure timestamps are datetime objects
    # AssetResponse should already have datetime objects, but handle edge cases
    if file_created_at is None:
        file_created_at = datetime.now()
    elif not isinstance(file_created_at, datetime):
        # If it's not already a datetime (e.g., it's a string), parse it
        try:
            if isinstance(file_created_at, str):
                iso_string: str = file_created_at.replace("Z", "+00:00")
                file_created_at = datetime.fromisoformat(iso_string)
            else:
                file_created_at = datetime.now()
        except (ValueError, AttributeError):
            file_created_at = datetime.now()

    if file_modified_at is None:
        file_modified_at = datetime.now()
    elif not isinstance(file_modified_at, datetime):
        # If it's not already a datetime (e.g., it's a string), parse it
        try:
            if isinstance(file_modified_at, str):
                iso_string: str = file_modified_at.replace("Z", "+00:00")
                file_modified_at = datetime.fromisoformat(iso_string)
            else:
                file_modified_at = datetime.now()
        except (ValueError, AttributeError):
            file_modified_at = datetime.now()

    # Determine asset type based on MIME type
    asset_type = (
        AssetTypeEnum.IMAGE if mime_type.startswith("image/") else AssetTypeEnum.VIDEO
    )

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
        localDateTime=file_created_at,
        updatedAt=file_modified_at,
        checksum=checksum or "placeholder-checksum",
        exifInfo=exif_info,  # Now includes processed EXIF data
        createdAt=file_created_at,
        duration="00:00:00.000000" if asset_type == AssetTypeEnum.VIDEO else "",
        hasMetadata=True,
        isArchived=False,
        isFavorite=False,
        isOffline=False,
        isTrashed=False,
        originalPath=f"/gumnut/assets/{asset_id}",
        ownerId=current_user.id,
        owner=current_user,
        thumbhash="",
        visibility=AssetVisibility.timeline,
        people=people,
    )
