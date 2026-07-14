"""
Converter functions mapping Gumnut SDK types to Immich sync models.

Pure functions with no internal dependencies.
"""

from uuid import UUID

from gumnut.types.album_asset_response import AlbumAssetResponse
from gumnut.types.album_response import AlbumResponse
from gumnut.types.asset_response import AssetResponse
from gumnut.types.face_response import FaceResponse
from gumnut.types.person_response import PersonResponse
from gumnut.types.user_response import UserResponse

from routers.immich_models import (
    AlbumUserRole,
    AssetOrder,
    AssetVisibility,
    SyncAlbumToAssetV1,
    SyncAlbumUserV1,
    SyncAlbumV1,
    SyncAlbumV2,
    SyncAssetExifV1,
    SyncAssetFaceV1,
    SyncAssetFaceV2,
    SyncAssetV1,
    SyncAssetV2,
    SyncAuthUserV1,
    SyncPersonV1,
    SyncUserV1,
)
from routers.utils.asset_conversion import (
    duration_ms,
    exif_dims_and_orientation,
    format_duration,
    mime_type_to_asset_type,
    normalize_rating,
    resolve_file_created_at,
    resolve_file_modified_at,
    resolve_immich_checksum,
    resolve_local_date_time,
)
from routers.utils.current_user import map_user_quota
from routers.utils.datetime_utils import (
    format_timezone_immich,
    to_actual_utc,
)
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_album_id,
    safe_uuid_from_asset_id,
    safe_uuid_from_face_id,
    safe_uuid_from_person_id,
    safe_uuid_from_user_id,
)


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

    # Same per-user storage caps as GET/PUT /api/users/me, kept consistent across
    # both quota surfaces. quotaUsageInBytes is a required int here, so a missing
    # upstream value (rollout) coerces to 0.
    quota = map_user_quota(user)

    return SyncAuthUserV1(
        id=safe_uuid_from_user_id(user.id),
        email=user.email or "",
        name=full_name,
        hasProfileImage=False,
        profileChangedAt=user.updated_at,
        isAdmin=user.is_superuser,
        oauthId="",
        quotaUsageInBytes=quota.usage_bytes if quota.usage_bytes is not None else 0,
        avatarColor=None,
        deletedAt=None,
        pinCode=None,
        quotaSizeInBytes=quota.size_bytes,
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
        id=safe_uuid_from_user_id(user.id),
        email=user.email or "",
        name=full_name,
        hasProfileImage=False,
        profileChangedAt=user.updated_at,
        avatarColor=None,
        deletedAt=None,
    )


def gumnut_asset_to_sync_asset_v1(asset: AssetResponse, owner_id: UUID) -> SyncAssetV1:
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

    fileCreatedAt = resolve_file_created_at(asset)
    fileModifiedAt = resolve_file_modified_at(asset)
    localDateTime = resolve_local_date_time(asset)

    return SyncAssetV1(
        id=safe_uuid_from_asset_id(asset.id),
        checksum=resolve_immich_checksum(asset),
        # "Uploaded to Immich at" — required on SyncAssetV1 in Immich v3.
        createdAt=asset.created_at,
        isFavorite=False,  # Gumnut doesn't track favorites
        isEdited=False,
        originalFileName=asset.original_file_name,
        ownerId=owner_id,
        type=asset_type,
        visibility=AssetVisibility.timeline,
        fileCreatedAt=fileCreatedAt,
        fileModifiedAt=fileModifiedAt,
        localDateTime=localDateTime,
        # Optional fields - use None when not available
        deletedAt=asset.trashed_at,
        duration=format_duration(asset.duration),
        height=asset.height if asset.height else None,
        libraryId=None,
        livePhotoVideoId=None,
        stackId=None,
        thumbhash=asset.thumbhash,
        width=asset.width if asset.width else None,
    )


def gumnut_asset_to_sync_asset_v2(asset: AssetResponse, owner_id: UUID) -> SyncAssetV2:
    """Convert Gumnut AssetResponse to Immich SyncAssetV2 format.

    SyncAssetV2 is SyncAssetV1 with ``duration`` as integer milliseconds instead
    of the interval string — the only payload difference between the two (see the
    ``immich-v3-api-changes.md`` design doc, §5). Delegate to V1 and swap it.
    """
    fields = gumnut_asset_to_sync_asset_v1(asset, owner_id).model_dump()
    fields["duration"] = duration_ms(asset.duration)
    return SyncAssetV2(**fields)


def gumnut_metadata_to_sync_exif_v1(asset: AssetResponse) -> SyncAssetExifV1:
    """
    Convert Gumnut AssetResponse (with metadata) to Immich SyncAssetExifV1 format.

    Accepts the full AssetResponse because image dimensions live on the asset and
    file size lives on its nested ``file_data`` group — not on the Metadata object.

    Args:
        asset: Gumnut asset data (must have non-None metadata)

    Returns:
        SyncAssetExifV1 for sync stream
    """
    metadata = asset.metadata
    # Callers only pass assets that have metadata, so this is always non-None
    # at runtime. The check satisfies the type checker.
    if metadata is None:
        raise ValueError(
            f"Asset {asset.id} passed to metadata converter with no metadata"
        )

    # Convert datetimes to actual UTC for Immich compatibility
    original_datetime = to_actual_utc(metadata.original_datetime)
    modified_datetime = to_actual_utc(metadata.modified_datetime)

    raw_width, raw_height, wire_orientation = exif_dims_and_orientation(asset)
    file_size = asset.file_data.file_size_bytes if asset.file_data else None

    return SyncAssetExifV1(
        assetId=safe_uuid_from_asset_id(metadata.asset_id),
        city=metadata.city,
        country=metadata.country,
        dateTimeOriginal=original_datetime,
        description=metadata.description or "",
        exifImageHeight=raw_height,
        exifImageWidth=raw_width,
        exposureTime=_format_exposure_time(metadata.exposure_time),
        fNumber=metadata.f_number,
        fileSizeInByte=file_size,
        focalLength=metadata.focal_length,
        fps=metadata.fps,
        iso=metadata.iso,
        latitude=metadata.latitude,
        lensModel=metadata.lens_model,
        longitude=metadata.longitude,
        make=metadata.make,
        model=metadata.model,
        modifyDate=modified_datetime,
        orientation=wire_orientation,
        # profile_description is intentionally not surfaced on the Metadata
        # type (per the Gumnut API design); always emit None.
        profileDescription=None,
        projectionType=metadata.projection_type,
        rating=normalize_rating(metadata.rating),
        state=metadata.state,
        timeZone=format_timezone_immich(metadata.original_datetime),
    )


def gumnut_person_to_sync_person_v1(
    person: PersonResponse, owner_id: UUID
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
        id=safe_uuid_from_person_id(person.id),
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


def gumnut_album_to_sync_album_v1(album: AlbumResponse, owner_id: UUID) -> SyncAlbumV1:
    """Convert Gumnut AlbumResponse to Immich SyncAlbumV1 format."""
    thumbnail_asset_id = None
    if album.album_cover_asset_id:
        thumbnail_asset_id = str(safe_uuid_from_asset_id(album.album_cover_asset_id))

    return SyncAlbumV1(
        id=safe_uuid_from_album_id(album.id),
        ownerId=owner_id,
        name=album.name,
        description=album.description or "",
        createdAt=album.created_at,
        updatedAt=album.updated_at,
        thumbnailAssetId=thumbnail_asset_id,
        isActivityEnabled=True,
        order=AssetOrder.desc,
    )


def gumnut_album_to_sync_album_v2(album: AlbumResponse, owner_id: UUID) -> SyncAlbumV2:
    """Convert Gumnut AlbumResponse to Immich SyncAlbumV2 format.

    In the Immich v3 GA model SyncAlbumV2 is SyncAlbumV1 without ``ownerId``
    (see the ``immich-v3-api-changes.md`` design doc, §5). Delegate to V1 and
    drop the field V2 no longer carries.
    """
    fields = gumnut_album_to_sync_album_v1(album, owner_id).model_dump()
    fields.pop("ownerId", None)
    return SyncAlbumV2(**fields)


def gumnut_album_to_sync_album_user_v1(
    album: AlbumResponse, owner_id: UUID
) -> SyncAlbumUserV1:
    """Synthesize the owner album-user link for an album.

    Immich v3's ``SyncAlbumV2`` dropped ``ownerId`` (see the
    ``immich-v3-api-changes.md`` design doc, §5), so the v3 mobile client no
    longer derives the owner from the album event itself. It instead builds the
    album↔owner relationship from the separate ``AlbumUsersV1`` stream, and its
    album-list query inner-joins on an owner-role album-user row — without one,
    every album is filtered out of the list and never displayed. Gumnut is
    single-user with no album sharing, so each album has exactly one album-user:
    the owner.
    """
    return SyncAlbumUserV1(
        albumId=safe_uuid_from_album_id(album.id),
        userId=owner_id,
        role=AlbumUserRole.owner,
    )


def gumnut_face_to_sync_face_v1(face: FaceResponse) -> SyncAssetFaceV1:
    """Convert Gumnut FaceResponse to Immich SyncAssetFaceV1 format."""
    bounding_box = face.bounding_box

    person_id = None
    if face.person_id:
        person_id = str(safe_uuid_from_person_id(face.person_id))

    return SyncAssetFaceV1(
        id=safe_uuid_from_face_id(face.id),
        assetId=safe_uuid_from_asset_id(face.asset_id),
        boundingBoxX1=bounding_box.get("x", 0),
        boundingBoxX2=bounding_box.get("x", 0) + bounding_box.get("w", 0),
        boundingBoxY1=bounding_box.get("y", 0),
        boundingBoxY2=bounding_box.get("y", 0) + bounding_box.get("h", 0),
        imageHeight=0,
        imageWidth=0,
        sourceType="machine-learning",
        personId=person_id,
    )


def gumnut_face_to_sync_face_v2(face: FaceResponse) -> SyncAssetFaceV2:
    """Convert Gumnut FaceResponse to Immich SyncAssetFaceV2 format.

    Delegates to V1 and adds deletedAt (always None — Gumnut has no soft-delete
    on faces) and isVisible (always True — Gumnut has no face visibility control).
    """
    v1 = gumnut_face_to_sync_face_v1(face)
    return SyncAssetFaceV2(**v1.model_dump(), deletedAt=None, isVisible=True)


def gumnut_album_asset_to_sync_album_to_asset_v1(
    album_asset: AlbumAssetResponse,
) -> SyncAlbumToAssetV1:
    """Convert Gumnut AlbumAssetResponse to Immich SyncAlbumToAssetV1 format."""
    return SyncAlbumToAssetV1(
        albumId=safe_uuid_from_album_id(album_asset.album_id),
        assetId=safe_uuid_from_asset_id(album_asset.asset_id),
    )
