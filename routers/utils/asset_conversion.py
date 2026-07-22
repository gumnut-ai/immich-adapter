"""
Utility functions for converting Gumnut assets to Immich format.

This module provides shared functionality for converting asset data from the Gumnut API
to the Immich API format, including metadata (camera/EXIF/GPS/location) processing.
"""

import logging
from datetime import datetime
from uuid import UUID

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
from routers.utils.person_conversion import convert_gumnut_person_to_immich

logger = logging.getLogger(__name__)

# `include` sets the adapter must request explicitly on its Gumnut asset reads.
#
# The Gumnut API returns the full asset shape today, but is moving to a lean default
# where an omitted `include` returns none of the heavy fields. The adapter reads
# several of those off every asset, so it must opt back into exactly what its
# conversions consume — otherwise, once the default flips, EXIF / checksum / size
# / people silently become null and the Immich-facing asset is corrupted.
#
# `metadata` feeds the EXIF block (camera/GPS/timestamps); `file_data` feeds
# `checksum_sha1` → Immich checksum, `file_size_bytes`, and the
# `file_modified_at` cascade; `people` feeds `AssetResponseDto.people`.
#
# Deliberately omitted: `faces` (the adapter reads faces from the dedicated
# `/faces` endpoint, never off the asset), `metrics` (never read), and the
# `variants` token. `convert_gumnut_asset_to_immich` does not read `asset_urls`
# at all, so these data-field reads need no variant token. The byte-serving
# paths in `routers/api/assets.py` are the exception — they stream the
# non-thumbnail rungs (gated behind `variants`) and so request
# `include=variants` at their own call site.
ASSET_INCLUDE: list[str] = ["metadata", "people", "file_data"]
"""For reads that feed ``convert_gumnut_asset_to_immich`` (reads ``people``)."""

ASSET_INCLUDE_NO_PEOPLE: list[str] = ["metadata", "file_data"]
"""For the sync-stream converters, which read EXIF + the ``file_data`` scalars
(``checksum_sha1`` / ``file_size_bytes`` / ``file_modified_at``) but never
``people`` — skipping ``people`` avoids a server-side aggregation on the scan."""

ASSET_INCLUDE_METADATA_ONLY: list[str] = ["metadata"]
"""For reads that consume only ``metadata`` fields and no ``file_data`` scalar:
map markers (GPS) and the per-asset datetime rewrite (``original_datetime``)."""


def resolve_immich_checksum(gumnut_asset: AssetResponse) -> str:
    """Return the Immich-facing asset checksum (base64-encoded SHA-1).

    Immich's API contract for ``checksum`` is a base64-encoded **SHA-1** (28
    chars): clients compute the SHA-1 of a local file and compare it to this
    value for pre-upload dedup and for local↔remote asset linking ("merged"
    state) in the mobile client. Gumnut exposes that value as
    ``AssetResponse.file_data.checksum_sha1`` (the nested file/provenance
    group, requested via ``include=file_data``).

    Gumnut's other ``checksum`` field is a base64-encoded SHA-256 (44 chars).
    It must never be sent on this field: a wrong-format value can never equal
    the client-computed SHA-1, so it silently breaks dedup and makes a
    backed-up photo appear as two separate timeline entries.

    When ``checksum_sha1`` is null (rare legacy rows), return ``""`` rather
    than substituting the SHA-256 or a placeholder. An empty checksum yields a
    clean "no match" (a dedup false-negative — the documented Immich behavior)
    instead of a value that looks valid but never matches.
    """
    file_data = gumnut_asset.file_data
    checksum_sha1 = file_data.checksum_sha1 if file_data else None
    if checksum_sha1 is None:
        logger.warning(
            "Asset %s has no checksum_sha1; emitting empty Immich checksum",
            gumnut_asset.id,
            extra={"asset_id": gumnut_asset.id},
        )
        return ""
    return checksum_sha1


def normalize_rating(rating: float | int | None) -> int | None:
    """Bound a rating to the Immich DTO's valid 1-5 range, or None.

    ``ExifResponseDto.rating`` is constrained ``ge=1, le=5``, so any value
    outside that range must become None ("unrated") rather than reach the DTO
    and raise a ValidationError. Cameras write 0 (XMP:Rating) or the deprecated
    -1 to mean "unrated"; those, and any other out-of-range value, map to None.
    """
    if rating is None:
        return None
    value = int(float(rating))
    return value if 1 <= value <= 5 else None


def format_duration(seconds: float | None) -> str | None:
    """Format a video duration (float seconds) as Immich's ``HH:MM:SS.ffffff`` interval string.

    Retained for the ``SyncAssetV1`` sync payload, whose ``duration`` field is
    still the interval string in the Immich v3 spec. The asset/timeline REST
    responses moved to integer milliseconds — see ``duration_ms`` — but the v1
    sync entity did not (its int-ms successor is ``SyncAssetV2``, added by the
    Sync v2 layer). The Gumnut asset carries ``duration`` as float seconds, or
    ``None`` when the asset is an image or its duration has not been extracted
    yet; ``None`` passes through so each sync emit site preserves its
    absent-duration behavior rather than fabricating a value.
    """
    if seconds is None:
        return None
    # Round to whole microseconds first, then decompose, so a value just under a
    # minute/hour boundary (e.g. 59.9999999) carries up to 00:01:00.000000 rather
    # than rendering an out-of-range 00:00:60.000000.
    micros = round(float(seconds) * 1_000_000)
    secs_total, micros = divmod(micros, 1_000_000)
    hours, remainder = divmod(secs_total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{micros:06d}"


def duration_ms(seconds: float | None) -> int | None:
    """Convert an upstream video duration (float seconds) to Immich's integer milliseconds.

    Immich v3 expresses ``duration`` on ``AssetResponseDto`` and
    ``TimeBucketAssetResponseDto`` (and, via the Sync v2 layer, the
    ``SyncAssetV2`` payload) as integer **milliseconds**, nullable — ``null``
    for a static image or a video whose duration has not been extracted yet.
    The Gumnut asset carries ``duration`` as float seconds, so round to whole
    milliseconds; ``None`` passes through as ``None`` rather than fabricating a
    length (the nullable v3 field makes the old zero/empty-string placeholders
    unnecessary).
    """
    if seconds is None:
        return None
    return round(float(seconds) * 1000)


def resolve_capture_datetime(gumnut_asset: AssetResponse) -> datetime:
    """Return the capture datetime resolved by the Gumnut API for Immich timeline fields.

    The Gumnut API resolves ``local_datetime`` from
    ``metadata.original_datetime → file_created_at → created_at`` internally,
    so adapter callers treat it as the single source of truth and must not
    re-add a fallback chain here.
    """
    return gumnut_asset.local_datetime


def resolve_file_created_at(gumnut_asset: AssetResponse) -> datetime:
    """Return capture time formatted for Immich ``fileCreatedAt`` fields."""
    return to_actual_utc(resolve_capture_datetime(gumnut_asset))


def resolve_created_at(gumnut_asset: AssetResponse) -> datetime:
    """Return the Gumnut upload time (``created_at``) formatted for Immich ``createdAt`` fields."""
    return to_actual_utc(gumnut_asset.created_at)


def resolve_local_date_time(gumnut_asset: AssetResponse) -> datetime:
    """Return capture time formatted for Immich ``localDateTime`` fields."""
    return to_immich_local_datetime(resolve_capture_datetime(gumnut_asset))


def resolve_file_modified_at(gumnut_asset: AssetResponse) -> datetime:
    """Return file modified time formatted for Immich ``fileModifiedAt`` fields.

    Unlike capture time, the Gumnut API does not resolve a single modify-time
    field for us — ``file_data.file_modified_at`` is the raw file mtime. The
    adapter applies the ``metadata.modified_datetime → file_data.file_modified_at``
    cascade here so the EXIF modify time isn't lost on the wire.

    ``file_data.file_modified_at`` is nullable (``file_data`` is the nested
    file/provenance group, requested via ``include=file_data``), so the cascade
    ends in a final fall back to the capture time — Immich's ``fileModifiedAt``
    is required, so this must never return ``None``.
    """
    metadata_modified = (
        gumnut_asset.metadata.modified_datetime if gumnut_asset.metadata else None
    )
    file_data = gumnut_asset.file_data
    return (
        to_actual_utc(metadata_modified)
        or to_actual_utc(file_data.file_modified_at if file_data else None)
        or resolve_file_created_at(gumnut_asset)
    )


def exif_dims_and_orientation(
    gumnut_asset: AssetResponse,
) -> tuple[int | None, int | None, str | None]:
    """Return (exifImageWidth, exifImageHeight, wire_orientation) for the EXIF wire fields.

    The Gumnut API stores pre-rotation sensor dims on ``metadata.raw_width`` /
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

    Treats ``0`` as "unknown," the same as ``None``: assets where the Gumnut API
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
    file_size = (
        gumnut_asset.file_data.file_size_bytes if gumnut_asset.file_data else None
    )

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


def extract_sync_exif(gumnut_asset: AssetResponse, asset_uuid: UUID) -> SyncAssetExifV1:
    """
    Extract metadata from a Gumnut AssetResponse for sync events.

    Args:
        gumnut_asset: The Gumnut AssetResponse object
        asset_uuid: The asset UUID

    Returns:
        SyncAssetExifV1 object with metadata from the asset
    """
    metadata = gumnut_asset.metadata

    if metadata is None:
        make = None
        model = None
        lens_model = None
        f_number = None
        focal_length = None
        iso = None
        exposure_time = None
        latitude = None
        longitude = None
        city = None
        state = None
        country = None
        description = None
        rating = None
        projection_type = None
        date_time_original = None
        modify_date = None
    else:
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
        date_time_original = metadata.original_datetime
        modify_date = metadata.modified_datetime

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
    file_size = (
        gumnut_asset.file_data.file_size_bytes if gumnut_asset.file_data else None
    )

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
        fileSizeInByte=int(file_size) if file_size else None,
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
    gumnut_asset: AssetResponse, owner_id: UUID
) -> AssetUploadReadyV1Payload:
    """
    Build an AssetUploadReadyV1Payload from a Gumnut asset for WebSocket sync events.

    Args:
        gumnut_asset: The Gumnut AssetResponse object
        owner_id: The owner's user id (UUID form of the Gumnut user id)

    Returns:
        AssetUploadReadyV1Payload containing SyncAssetV1 and SyncAssetExifV1
    """
    asset_uuid = safe_uuid_from_asset_id(gumnut_asset.id)

    file_created_at = resolve_file_created_at(gumnut_asset)
    file_modified_at = resolve_file_modified_at(gumnut_asset)
    local_date_time = resolve_local_date_time(gumnut_asset)

    width = gumnut_asset.width
    height = gumnut_asset.height

    sync_asset = SyncAssetV1(
        id=asset_uuid,
        ownerId=owner_id,
        # "Uploaded to Immich at" — required on SyncAssetV1 in Immich v3.
        createdAt=gumnut_asset.created_at,
        thumbhash=gumnut_asset.thumbhash,
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
            people.append(convert_gumnut_person_to_immich(person))

    # Extract EXIF object directly from AssetResponse
    exif_info = extract_exif_info(gumnut_asset)

    width = gumnut_asset.width
    height = gumnut_asset.height

    return AssetResponseDto(
        id=safe_uuid_from_asset_id(asset_id),
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
        duration=duration_ms(gumnut_asset.duration),
        hasMetadata=True,
        height=height if height else None,
        isArchived=False,
        isEdited=False,
        isFavorite=False,
        isOffline=False,
        isTrashed=bool(gumnut_asset.trashed_at),
        originalPath=f"/gumnut/assets/{asset_id}",
        ownerId=current_user.id,
        owner=current_user,
        # Upstream base64 ThumbHash, or None until the encoder has run. None is
        # the normal not-yet-generated state and a legal value for the nullable
        # Immich field — clients simply skip the blur until it is backfilled.
        thumbhash=gumnut_asset.thumbhash,
        visibility=AssetVisibility.timeline,
        width=width if width else None,
        people=people,
    )
