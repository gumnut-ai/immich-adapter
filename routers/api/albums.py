from typing import List
from uuid import UUID
import logging

from fastapi import APIRouter, HTTPException, Query, Response

from routers.utils.gumnut_client import get_gumnut_client
from routers.utils.error_mapping import map_gumnut_error, check_for_error_by_code
from routers.immich_models import (
    AlbumResponseDto,
    BulkIdResponseDto,
    BulkIdsDto,
    CreateAlbumDto,
    AlbumsAddAssetsDto,
    AlbumsAddAssetsResponseDto,
    BulkIdErrorReason,
    AlbumStatisticsResponseDto,
    UpdateAlbumDto,
    UpdateAlbumUserDto,
    AddUsersDto,
    Error2,
)
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
)
from routers.utils.asset_conversion import convert_gumnut_asset_to_immich
from routers.utils.album_conversion import convert_gumnut_album_to_immich

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/albums",
    tags=["albums"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def get_all_albums(
    asset_id: UUID = Query(default=None, alias="assetId"),
    shared: bool = Query(default=None, alias="shared"),
) -> List[AlbumResponseDto]:
    """
    Fetch albums from Gumnut and convert to AlbumResponseDto format.
    Shared albums are not supported in this adapter and will return an empty list.
    """
    client = get_gumnut_client()

    if shared:
        # Shared albums not supported in this adapter
        return []

    try:
        kwargs = {}
        if asset_id:
            kwargs["asset_id"] = uuid_to_gumnut_asset_id(asset_id)

        gumnut_albums = client.albums.list(**kwargs)

        # Convert Gumnut albums to AlbumResponseDto format
        immich_albums = [
            convert_gumnut_album_to_immich(album, asset_count=album.asset_count)
            for album in gumnut_albums
        ]

        return immich_albums

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch albums")


@router.get("/statistics")
async def get_album_statistics() -> AlbumStatisticsResponseDto:
    """
    Get album statistics from Gumnut.
    Since Gumnut doesn't support shared albums, all albums are considered owned and not shared.
    """
    client = get_gumnut_client()

    try:
        # Get all albums to count them
        gumnut_albums = client.albums.list()

        # Count albums by converting SyncCursorPage to list
        albums_list = list(gumnut_albums)
        total_albums = len(albums_list)

        # Since Gumnut doesn't support shared albums, all albums are:
        # - owned by the current user
        # - not shared
        return AlbumStatisticsResponseDto(
            notShared=total_albums,  # All albums are not shared
            owned=total_albums,  # All albums are owned by current user
            shared=0,  # No shared albums in Gumnut
        )

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch album statistics")


@router.get("/{id}")
async def get_album_info(
    id: UUID,
    withoutAssets: bool = Query(default=None, alias="withoutAssets"),
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
) -> AlbumResponseDto:
    """
    Fetch a specific album from Gumnut and convert to AlbumResponseDto format.
    If withoutAssets is False, also fetch and include the album's assets.
    """
    client = get_gumnut_client()

    try:
        gumnut_album_id = uuid_to_gumnut_album_id(id)

        # Retrieve the specific album from Gumnut
        gumnut_album = client.albums.retrieve(gumnut_album_id)

        # Also retrieve the assets for this album
        try:
            gumnut_assets_response = client.albums.assets.list(gumnut_album_id)
            # The response should be iterable (like a list)
            gumnut_assets = list(gumnut_assets_response)
        except Exception as assets_error:
            # If assets retrieval fails, continue with empty assets list
            logger.warning(
                f"Warning: Could not retrieve assets for album {gumnut_album_id}: {assets_error}"
            )
            gumnut_assets = []

        # Convert assets to AssetResponseDto format
        immich_assets = []
        if not withoutAssets and gumnut_assets:
            for gumnut_asset in gumnut_assets:
                try:
                    immich_asset = convert_gumnut_asset_to_immich(gumnut_asset)
                    immich_assets.append(immich_asset)
                except Exception as convert_error:
                    logger.warning(
                        f"Warning: Could not convert asset {gumnut_asset}: {convert_error}"
                    )

        # Set album thumbnail to first asset if available
        album_thumbnail_id = immich_assets[0].id if immich_assets else None

        # Convert Gumnut album to AlbumResponseDto format using utility function
        immich_album = convert_gumnut_album_to_immich(
            gumnut_album,
            assets=immich_assets,
            asset_count=gumnut_album.asset_count,
            album_thumbnail_id=album_thumbnail_id,
        )

        return immich_album

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch album")


@router.post("", status_code=201)
async def create_album(request: CreateAlbumDto) -> AlbumResponseDto:
    """
    Create a new album using the Gumnut SDK.
    Note: albumUsers and assetIds are not supported by the Gumnut SDK.
    """
    client = get_gumnut_client()

    try:
        album_name = request.albumName or ""

        # Create the album
        gumnut_album = client.albums.create(
            name=album_name,
            description=request.description,
            # Note: albumUsers and assetIds are not supported in this adapter
        )

        # Convert Gumnut album to AlbumResponseDto format using utility function
        immich_album = convert_gumnut_album_to_immich(gumnut_album, asset_count=0)

        return immich_album

    except Exception as e:
        raise map_gumnut_error(e, "Failed to create album")


@router.put("/{id}/assets")
async def add_assets_to_album(
    id: UUID,
    request: BulkIdsDto,
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
) -> List[BulkIdResponseDto]:
    """
    Add assets to an album using the Gumnut SDK.
    Returns a list of results indicating success/failure for each asset.
    """
    client = get_gumnut_client()

    try:
        gumnut_album_id = uuid_to_gumnut_album_id(id)

        # Verify album exists first
        try:
            client.albums.retrieve(gumnut_album_id)
        except Exception as e:
            if check_for_error_by_code(e, 404):
                raise HTTPException(
                    status_code=404,
                    detail=f"Album not found {id} -> {gumnut_album_id}",
                )
            raise  # Re-raise other exceptions

        # Process each asset ID
        response = []

        for asset_uuid in request.ids:
            asset_uuid_str = str(asset_uuid)
            try:
                gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)

                # Add asset to album using Gumnut SDK
                client.albums.assets.add(gumnut_album_id, asset_ids=[gumnut_asset_id])

                # Success response
                response.append(BulkIdResponseDto(id=asset_uuid_str, success=True))

            except Exception as asset_error:
                # Handle individual asset errors
                error_msg = str(asset_error).lower()
                if "duplicate" in error_msg or "already exists" in error_msg:
                    response.append(
                        BulkIdResponseDto(
                            id=asset_uuid_str, success=False, error=Error2.duplicate
                        )
                    )
                elif (
                    check_for_error_by_code(asset_error, 404)
                    or "not found" in error_msg
                ):
                    response.append(
                        BulkIdResponseDto(
                            id=asset_uuid_str, success=False, error=Error2.not_found
                        )
                    )
                else:
                    response.append(
                        BulkIdResponseDto(
                            id=asset_uuid_str, success=False, error=Error2.unknown
                        )
                    )

        return response

    except HTTPException:
        # Re-raise HTTP exceptions (like 404 for album not found)
        raise
    except Exception as e:
        raise map_gumnut_error(e, "Failed to update album assets")


@router.patch("/{id}")
async def update_album(
    id: UUID,
    request: UpdateAlbumDto,
) -> AlbumResponseDto:
    """
    Update an album using the Gumnut SDK.
    Only name and description are supported by the Gumnut SDK.
    """
    client = get_gumnut_client()

    try:
        gumnut_album_id = uuid_to_gumnut_album_id(id)

        # Verify album exists first
        try:
            current_album = client.albums.retrieve(gumnut_album_id)
        except Exception as e:
            if check_for_error_by_code(e, 404):
                raise HTTPException(status_code=404, detail="Album not found")
            raise  # Re-raise other exceptions

        # Prepare update parameters
        update_params = {}
        if request.albumName is not None:
            update_params["name"] = request.albumName
        if request.description is not None:
            update_params["description"] = request.description

        # Only call update if there are supported parameters to update
        if update_params:
            updated_album = client.albums.update(gumnut_album_id, **update_params)
        else:
            # No supported updates, return current album
            updated_album = current_album

        # Convert Gumnut album to AlbumResponseDto format using utility function
        immich_album = convert_gumnut_album_to_immich(updated_album, asset_count=0)

        return immich_album

    except HTTPException:
        # Re-raise HTTP exceptions (like 404 for album not found)
        raise
    except Exception as e:
        raise map_gumnut_error(e, "Failed to update album")


@router.delete("/{id}/assets")
async def remove_asset_from_album(
    id: UUID,
    request: BulkIdsDto,
) -> List[BulkIdResponseDto]:
    """
    Remove assets from an album using the Gumnut SDK.
    Returns a list of results indicating success/failure for each asset removal.
    """
    client = get_gumnut_client()

    try:
        gumnut_album_id = uuid_to_gumnut_album_id(id)

        # Verify album exists first
        try:
            client.albums.retrieve(gumnut_album_id)
        except Exception as e:
            if check_for_error_by_code(e, 404):
                raise HTTPException(
                    status_code=404,
                    detail=f"Album not found {id} -> {gumnut_album_id}",
                )
            raise  # Re-raise other exceptions

        # Process each asset ID
        response = []

        for asset_uuid in request.ids:
            asset_uuid_str = str(asset_uuid)
            try:
                gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)

                # Remove asset from album using Gumnut SDK
                client.albums.assets.remove(
                    gumnut_album_id, asset_ids=[gumnut_asset_id]
                )

                # Success response
                response.append(BulkIdResponseDto(id=asset_uuid_str, success=True))

            except Exception as asset_error:
                # Handle individual asset errors
                error_msg = str(asset_error).lower()
                if (
                    check_for_error_by_code(asset_error, 404)
                    or "not found" in error_msg
                ):
                    response.append(
                        BulkIdResponseDto(
                            id=asset_uuid_str, success=False, error=Error2.not_found
                        )
                    )
                elif "not in album" in error_msg or "not member" in error_msg:
                    response.append(
                        BulkIdResponseDto(
                            id=asset_uuid_str, success=False, error=Error2.not_found
                        )
                    )
                else:
                    response.append(
                        BulkIdResponseDto(
                            id=asset_uuid_str, success=False, error=Error2.unknown
                        )
                    )

        return response

    except HTTPException:
        # Re-raise HTTP exceptions (like 404 for album not found)
        raise
    except Exception as e:
        raise map_gumnut_error(e, "Failed to remove album assets")


@router.delete("/{id}", status_code=204)
async def delete_album(id: UUID) -> Response:
    """
    Delete an album using the Gumnut SDK.
    """
    client = get_gumnut_client()

    try:
        gumnut_album_id = uuid_to_gumnut_album_id(id)

        # Verify album exists first
        try:
            client.albums.retrieve(gumnut_album_id)
        except Exception as e:
            if check_for_error_by_code(e, 404):
                raise HTTPException(status_code=404, detail="Album not found")
            raise  # Re-raise other exceptions

        # Delete the album using Gumnut SDK
        client.albums.delete(gumnut_album_id)

        # Return 204 No Content response
        return Response(status_code=204)

    except HTTPException:
        # Re-raise HTTP exceptions (like 404 for album not found)
        raise
    except Exception as e:
        raise map_gumnut_error(e, "Failed to delete album")


@router.put("/assets")
async def add_assets_to_albums(
    request: AlbumsAddAssetsDto,
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
) -> AlbumsAddAssetsResponseDto:
    """
    Add assets to multiple albums using the Gumnut SDK.
    Returns a single result indicating overall success/failure.
    """
    client = get_gumnut_client()

    try:
        gumnut_asset_ids = [
            uuid_to_gumnut_asset_id(asset_uuid) for asset_uuid in request.assetIds
        ]

        successful_operations = 0
        total_operations = len(request.albumIds)
        first_error = None

        for album_uuid in request.albumIds:
            try:
                gumnut_album_id = uuid_to_gumnut_album_id(album_uuid)

                # Verify album exists first
                try:
                    client.albums.retrieve(gumnut_album_id)
                except Exception as e:
                    if check_for_error_by_code(e, 404):
                        if first_error is None:
                            first_error = BulkIdErrorReason.not_found
                        continue
                    raise  # Re-raise other exceptions

                # Add assets to album using Gumnut SDK
                client.albums.assets.add(gumnut_album_id, asset_ids=gumnut_asset_ids)
                successful_operations += 1

            except Exception as album_error:
                # Handle individual album errors
                error_msg = str(album_error).lower()
                if first_error is None:
                    if (
                        check_for_error_by_code(album_error, 404)
                        or "not found" in error_msg
                    ):
                        first_error = BulkIdErrorReason.not_found
                    elif "duplicate" in error_msg or "already exists" in error_msg:
                        first_error = BulkIdErrorReason.duplicate
                    else:
                        first_error = BulkIdErrorReason.unknown

        # Return success only if all operations succeeded
        if successful_operations == total_operations:
            return AlbumsAddAssetsResponseDto(success=True)
        else:
            return AlbumsAddAssetsResponseDto(
                success=False, error=first_error or BulkIdErrorReason.unknown
            )

    except Exception as e:
        raise map_gumnut_error(e, "Failed to add assets to albums")


@router.delete("/{id}/user/{userId}", status_code=204)
async def remove_user_from_album(id: UUID, userId: str) -> Response:
    """
    Remove a user from an album.
    This is a stub implementation as user functionality is not currently supported.
    """
    raise HTTPException(
        status_code=501,
        detail="User functionality is not supported in this adapter. Albums cannot be shared.",
    )


@router.put("/{id}/user/{userId}", status_code=204)
async def update_album_user(
    id: UUID, userId: str, request: UpdateAlbumUserDto
) -> Response:
    """
    Update a user's role in an album.
    This is a stub implementation as user functionality is not currently supported.
    """
    raise HTTPException(
        status_code=501,
        detail="User functionality is not supported in this adapter. Albums cannot be shared.",
    )


@router.put("/{id}/users")
async def add_users_to_album(id: UUID, request: AddUsersDto) -> AlbumResponseDto:
    """
    Add users to an album.
    This is a stub implementation as user functionality is not currently supported.
    """
    raise HTTPException(
        status_code=501,
        detail="User functionality is not supported in this adapter. Albums cannot be shared.",
    )
