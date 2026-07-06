"""
Utility functions for converting Gumnut albums to Immich format.

This module provides shared functionality for converting album data from the Gumnut API
to the Immich API format, including handling of datetime fields and album metadata.
"""

from gumnut.types.album_response import AlbumResponse
from routers.immich_models import (
    AlbumResponseDto,
    AlbumUserResponseDto,
    AlbumUserRole,
    AssetOrder,
    UserResponseDto,
)
from routers.utils.datetime_utils import to_immich_local_datetime
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_album_id,
    safe_uuid_from_asset_id,
)


def convert_gumnut_album_to_immich(
    gumnut_album: AlbumResponse,
    current_user: UserResponseDto,
    asset_count: int | None = None,
) -> AlbumResponseDto:
    """
    Convert a Gumnut album to AlbumResponseDto format.

    Args:
        gumnut_album: The Gumnut AlbumResponse object
        current_user: The current user's UserResponseDto
        asset_count: Asset count (defaults to 0 if None)

    Returns:
        AlbumResponseDto object with processed data
    """
    album_id = gumnut_album.id
    album_name = gumnut_album.name
    album_description = gumnut_album.description
    created_at = gumnut_album.created_at
    updated_at = gumnut_album.updated_at

    final_asset_count = asset_count if asset_count is not None else 0

    return AlbumResponseDto(
        id=str(safe_uuid_from_album_id(album_id)),
        albumName=album_name,
        description=album_description,  # type: ignore
        albumThumbnailAssetId=str(
            safe_uuid_from_asset_id(gumnut_album.album_cover_asset_id)
        )
        if gumnut_album.album_cover_asset_id
        else "",
        createdAt=created_at,
        updatedAt=updated_at,
        # An album's start/end date is the min/max of its assets' local capture
        # datetimes, which the Gumnut API serializes timezone-naive when the
        # capture timezone is unknown. AlbumResponseDto.startDate/endDate require
        # timezone-aware values, so route them through the same keep-local-time
        # helper used for each asset's localDateTime, keeping the album's date
        # range consistent with the dates shown on its assets.
        startDate=to_immich_local_datetime(gumnut_album.start_date),
        endDate=to_immich_local_datetime(gumnut_album.end_date),
        lastModifiedAssetTimestamp=None,
        # Immich v3 derives the album owner from albumUsers[0] and no longer
        # carries owner/ownerId or inline assets. Gumnut has no album sharing,
        # so this is always exactly the single owner entry.
        albumUsers=[AlbumUserResponseDto(role=AlbumUserRole.owner, user=current_user)],
        shared=False,
        hasSharedLink=False,
        assetCount=final_asset_count,
        isActivityEnabled=True,
        order=AssetOrder.desc,
    )
