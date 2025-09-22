"""
Utility functions for converting Gumnut albums to Immich format.

This module provides shared functionality for converting album data from the Gumnut API
to the Immich API format, including handling of datetime fields and album metadata.
"""

from datetime import datetime
from uuid import UUID

from gumnut.types.album_response import AlbumResponse
from routers.api.auth import get_current_user_id
from routers.immich_models import AlbumResponseDto, AssetOrder, AssetResponseDto
from routers.utils.create_user_response import create_user_response_dto
from routers.utils.gumnut_id_conversion import safe_uuid_from_album_id


def convert_gumnut_album_to_immich(
    gumnut_album: AlbumResponse,
    assets: list[AssetResponseDto] | None = None,
    asset_count: int | None = None,
    album_thumbnail_id: UUID | None = None,
) -> AlbumResponseDto:
    """
    Convert a Gumnut album to AlbumResponseDto format.

    Args:
        gumnut_album: The Gumnut AlbumResponse object
        assets: Optional list of AssetResponseDto objects to include
        asset_count: Optional asset count (if None, will use len(assets) or album's asset_count)
        album_thumbnail_id: Optional thumbnail asset ID

    Returns:
        AlbumResponseDto object with processed data
    """
    album_id = gumnut_album.id
    album_name = gumnut_album.name or "Untitled Album"
    album_description = gumnut_album.description
    created_at = gumnut_album.created_at
    updated_at = gumnut_album.updated_at

    # Ensure created_at and updated_at are datetime objects
    # AlbumResponse should already have datetime objects, but handle edge cases
    if created_at is None:
        created_at = datetime.now()
    elif not isinstance(created_at, datetime):
        # If it's not already a datetime (e.g., it's a string), parse it
        try:
            if isinstance(created_at, str):
                iso_string: str = created_at.replace("Z", "+00:00")
                created_at = datetime.fromisoformat(iso_string)
            else:
                created_at = datetime.now()
        except (ValueError, AttributeError):
            created_at = datetime.now()

    if updated_at is None:
        updated_at = datetime.now()
    elif not isinstance(updated_at, datetime):
        # If it's not already a datetime (e.g., it's a string), parse it
        try:
            if isinstance(updated_at, str):
                iso_string: str = updated_at.replace("Z", "+00:00")
                updated_at = datetime.fromisoformat(iso_string)
            else:
                updated_at = datetime.now()
        except (ValueError, AttributeError):
            updated_at = datetime.now()

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
        albumThumbnailAssetId=str(album_thumbnail_id) if album_thumbnail_id else "",
        createdAt=created_at,
        updatedAt=updated_at,
        startDate=None,
        endDate=None,
        lastModifiedAssetTimestamp=None,
        ownerId=str(get_current_user_id()),
        owner=create_user_response_dto(),
        albumUsers=[],
        shared=False,
        hasSharedLink=False,
        assets=final_assets,
        assetCount=final_asset_count,
        isActivityEnabled=True,
        order=AssetOrder.desc,
    )
