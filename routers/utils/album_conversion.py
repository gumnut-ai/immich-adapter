"""
Utility functions for converting Gumnut albums to Immich format.

This module provides shared functionality for converting album data from the Gumnut API
to the Immich API format, including handling of datetime fields and album metadata.
"""

from gumnut.types.album_response import AlbumResponse
from routers.immich_models import (
    AlbumResponseDto,
    AssetOrder,
    AssetResponseDto,
    UserResponseDto,
)
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_album_id,
    safe_uuid_from_asset_id,
)


def convert_gumnut_album_to_immich(
    gumnut_album: AlbumResponse,
    current_user: UserResponseDto,
    assets: list[AssetResponseDto] | None = None,
    asset_count: int | None = None,
) -> AlbumResponseDto:
    """
    Convert a Gumnut album to AlbumResponseDto format.

    Args:
        gumnut_album: The Gumnut AlbumResponse object
        current_user: The current user's UserResponseDto
        assets: Optional list of AssetResponseDto objects to include
        asset_count: Optional asset count (if None, will use len(assets) or album's asset_count)

    Returns:
        AlbumResponseDto object with processed data
    """
    album_id = gumnut_album.id
    album_name = gumnut_album.name
    album_description = gumnut_album.description
    created_at = gumnut_album.created_at
    updated_at = gumnut_album.updated_at

    # Determine asset count
    if asset_count is not None:
        final_asset_count = asset_count
    elif assets is not None:
        final_asset_count = len(assets)
    else:
        final_asset_count = 0

    # Use provided assets or empty list
    final_assets = assets or []

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
        startDate=gumnut_album.start_date,
        endDate=gumnut_album.end_date,
        lastModifiedAssetTimestamp=None,
        ownerId=current_user.id,
        owner=current_user,
        albumUsers=[],
        shared=False,
        hasSharedLink=False,
        assets=final_assets,
        assetCount=final_asset_count,
        isActivityEnabled=True,
        order=AssetOrder.desc,
    )
