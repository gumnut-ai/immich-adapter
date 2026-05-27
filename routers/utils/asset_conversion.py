"""
Utility functions for converting Gumnut assets to Immich format.

This module provides shared functionality for converting asset data from the Gumnut API
to the Immich API format, including metadata (camera/EXIF/GPS/location) processing.
"""

import logging
from datetime import datetime

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

logger = logging.getLogger(__name__)


def resolve_immich_checksum(gumnut_asset: AssetResponse) -> str:
    """Return the Immich-facing asset checksum (base64-encoded SHA-1).

    Immich's API contract for ``checksum`` is a base64-encoded **SHA-1** (28
    chars): clients compute the SHA-1 of a local file and compare it to this
    value for pre-upload dedup and for local↔remote asset linking ("merged"
    state) in the mobile client. Gumnut exposes that value as
    ``AssetResponse.checksum_sha1``.

    Gumnut's other ``checksum`` field is a base64-encoded SHA-256 (44 chars).
    It must never be sent on this field: a wrong-format value can never equal
    the client-computed SHA-1, so it silently breaks dedup and makes a
    backed-up photo appear as two separate timeline entries.

    When ``checksum_sha1`` is null (rare legacy rows), return ``""`` rather
    than substituting the SHA-256 or a placeholder. An empty checksum yields a
    clean "no match" (a dedup false-negative — the documented Immich behavior)
    instead of a value that looks valid but never matches.
    """
    if gumnut_asset.checksum_sha1 is None:
        logger.warning(
            "Asset %s has no checksum_sha1; emitting empty Immich checksum",
            gumnut_asset.id,
            extra={"asset_id": gumnut_asset.id},
        )
        return ""
    return gumnut_asset.checksum_sha1


def normalize_rating(rating: float | int | None) -> int | None:
    """Normalize a rating value: convert -1 (deprecated 'unrated') to None."""
    if rating is None:
        return None
    value = int(float(rating))
    return None if value == -1 else value


def format_duration(seconds: float | None) -> str | None:
    """Format an upstream video duration (float seconds) as Immich's interval string.

    Immich expresses video duration as an ``HH:MM:SS.ffffff`` string. The Gumnut
    asset carries ``duration`` as float seconds, or ``None`` when the asset is an
    image or its duration has not been extracted yet. Returns ``None`` for ``None``
    so callers can preserve each emit site's existing absent-duration behavior
    rather than fabricating a value.
    """
    if seconds is None:
        return None
    total = float(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:09.6f}"


def _resolve_single_asset_duration(
    gumnut_asset: AssetResponse, asset_type: AssetTypeEnum
) -> str:
    """Duration string for the single-asset ``AssetResponseDto`` (non-nullable).

    Uses the upstream value when present; when absent, preserves the prior
    placeholder — zero duration for videos, empty string for images — rather
    than fabricating a length.
    """
    formatted = format_duration(gumnut_asset.duration)
    if formatted is not None:
        return formatted
    return "00:00:00.000000" if asset_type == AssetTypeEnum.VIDEO else ""


def resolve_capture_datetime(gumnut_asset: AssetResponse) -> datetime:
    """Return the Photos API-resolved capture datetime for Immich timeline fields.

    Photos API resolves ``local_datetime`` from
    ``metadata.original_datetime → file_created_at → created_at`` internally,
    so adapter callers treat it as the single source of truth and must not
    re-add a fallback chain here.
    """
    return gumnut_asset.local_datetime


def resolve_file_created_at(gumnut_asset: AssetResponse) -> datetime:
    """Return capture time formatted for Immich ``fileCreatedAt`` fields."""
    return to_actual_utc(resolve_capture_datetime(gumnut_asset))


def resolve_local_date_time(gumnut_asset: AssetResponse) -> datetime:
    """Return capture time formatted for Immich ``localDateTime`` fields."""
    return to_immich_local_datetime(resolve_capture_datetime(gumnut_asset))


def resolve_file_modified_at(gumnut_asset: AssetResponse) -> datetime:
    """Return file modified time formatted for Immich ``fileModifiedAt`` fields.

    Unlike capture time, photos-api does not resolve a single modify-time
    field for us — ``file_modified_at`` is the raw file mtime. The adapter
    applies the ``metadata.modified_datetime → file_modified_at`` cascade
    here so the EXIF modify time isn't lost on the wire.
    """
    metadata_modified = (
        gumnut_asset.metadata.modified_datetime if gumnut_asset.metadata else None
    )
    return to_actual_utc(metadata_modified) or to_actual_utc(
        gumnut_asset.file_modified_at
    )


def exif_dims_and_orientation(
    gumnut_asset: AssetResponse,
) -> tuple[int | None, int | None, str | None]:
    """Return (exifImageWidth, exifImageHeight, wire_orientation) for the EXIF wire fields.

    photos-api stores pre-rotation sensor dims on ``metadata.raw_width`` /
    ``raw_height``. When both are present, the EXIF orientation tag is
    emitted as-is — Immich mobile pairs the raw dims with the orientation
    and computes display dims locally via the 5–8 swap.

    Drift-cohort rows (ingested before that extraction was added, or files
    without ``ExifImageWidth/Height`` tags) have those columns NULL; in
    that case fall back to ``gumnut_asset.width/height``, which is already
    display-space for that cohort, and null the orientation tag on the
    wire. Feeding mobile display-space dims plus a non-null portrait
    orientation makes it re-apply the 5–8 swap and derive landscape dims
    — the same double-rotation class of bug the deleted ``wire_orientation``
    helper was guarding against. This is the only safe way to surface
    drift-cohort assets to mobile.

    Treats ``0`` as "unknown," the same as ``None``: assets where photos-api
    stores ``0`` for unknown dims (notably videos without EXIF width/height
    tags) must not surface ``0`` on the wire. The Immich mobile asset viewer
    computes ``width / height`` to size the viewport and only guards against
    ``null`` — a ``0/0`` ratio yields ``NaN`` and crashes the viewer.
    """
    metadata = gumnut_asset.metadata
    if metadata is not None:
        raw_w = metadata.raw_width
        raw_h = metadata.raw_height
        if raw_w and raw_h:
            orientation = metadata.orientation
            return (
                raw_w,
                raw_h,
                str(orientation) if orientation is not None else None,
            )
    if gumnut_asset.width and gumnut_asset.height:
        return gumnut_asset.width, gumnut_asset.height, None
    return None, None, None


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

    raw_width, raw_height, wire_orientation = exif_dims_and_orientation(gumnut_asset)
    file_size = gumnut_asset.file_size_bytes

    return ExifResponseDto(
        # Image dimensions
        exifImageWidth=int(float(raw_width)) if raw_width else None,
        exifImageHeight=int(float(raw_height)) if raw_height else None,
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
        orientation=wire_orientation,
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

    raw_width, raw_height, wire_orientation = exif_dims_and_orientation(gumnut_asset)

    return SyncAssetExifV1(
        assetId=asset_uuid,
        city=str(city) if city else None,
        country=str(country) if country else None,
        dateTimeOriginal=date_time_original,
        description=str(description) if description else None,
        exifImageHeight=int(raw_height) if raw_height else None,
        exifImageWidth=int(raw_width) if raw_width else None,
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
        orientation=wire_orientation,
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

    file_created_at = resolve_file_created_at(gumnut_asset)
    file_modified_at = resolve_file_modified_at(gumnut_asset)
    local_date_time = resolve_local_date_time(gumnut_asset)

    width = gumnut_asset.width
    height = gumnut_asset.height

    sync_asset = SyncAssetV1(
        id=asset_uuid,
        ownerId=owner_id,
        thumbhash=None,
        checksum=resolve_immich_checksum(gumnut_asset),
        deletedAt=gumnut_asset.trashed_at,
        duration=format_duration(gumnut_asset.duration),
        fileCreatedAt=file_created_at,
        fileModifiedAt=file_modified_at,
        height=int(height) if height else None,
        isEdited=False,
        isFavorite=False,
        libraryId=None,
        livePhotoVideoId=None,
        localDateTime=local_date_time,
        originalFileName=gumnut_asset.original_file_name or "",
        stackId=None,
        type=mime_type_to_asset_type(gumnut_asset.mime_type),
        visibility=AssetVisibility.timeline,
        width=int(width) if width else None,
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

    file_created_at = resolve_file_created_at(gumnut_asset)
    file_modified_at = resolve_file_modified_at(gumnut_asset)
    local_date_time = resolve_local_date_time(gumnut_asset)

    # Determine asset type based on MIME type
    asset_type = mime_type_to_asset_type(mime_type)

    people = []
    if gumnut_asset.people:
        for person in gumnut_asset.people:
            people.append(convert_gumnut_person_to_immich_with_faces(person))

    # Extract EXIF object directly from AssetResponse
    exif_info = extract_exif_info(gumnut_asset)

    width = gumnut_asset.width
    height = gumnut_asset.height

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
        updatedAt=gumnut_asset.updated_at,
        checksum=resolve_immich_checksum(gumnut_asset),
        exifInfo=exif_info,  # Now includes processed EXIF data
        createdAt=gumnut_asset.created_at,
        duration=_resolve_single_asset_duration(gumnut_asset, asset_type),
        hasMetadata=True,
        height=float(height) if height else None,
        isArchived=False,
        isEdited=False,
        isFavorite=False,
        isOffline=False,
        isTrashed=bool(gumnut_asset.trashed_at),
        originalPath=f"/gumnut/assets/{asset_id}",
        ownerId=current_user.id,
        owner=current_user,
        thumbhash="",
        visibility=AssetVisibility.timeline,
        width=float(width) if width else None,
        people=people,
    )
