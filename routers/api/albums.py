from typing import Annotated, List
from uuid import UUID
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from gumnut import APIStatusError, AsyncGumnut, ConflictError, GumnutError

from routers.utils.error_mapping import (
    classify_bulk_item_error,
    log_bulk_transport_error,
)
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.current_user import get_current_user
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
    Error1,
    UserResponseDto,
)
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
)
from routers.utils.asset_conversion import convert_gumnut_asset_to_immich
from routers.utils.album_conversion import convert_gumnut_album_to_immich
from pydantic.json_schema import SkipJsonSchema

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/albums",
    tags=["albums"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def get_all_albums(
    asset_id: Annotated[UUID | SkipJsonSchema[None], Query(alias="assetId")] = None,
    shared: Annotated[bool | SkipJsonSchema[None], Query(alias="shared")] = None,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> List[AlbumResponseDto]:
    """
    Fetch albums from Gumnut and convert to AlbumResponseDto format.
    Shared albums are not supported in this adapter and will return an empty list.
    """

    if shared:
        # Shared albums not supported in this adapter
        return []

    kwargs = {}
    if asset_id:
        kwargs["asset_id"] = uuid_to_gumnut_asset_id(asset_id)

    gumnut_albums = client.albums.list(**kwargs)

    return [
        convert_gumnut_album_to_immich(
            album, current_user, asset_count=album.asset_count
        )
        async for album in gumnut_albums
    ]


@router.get("/statistics")
async def get_album_statistics(
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> AlbumStatisticsResponseDto:
    """
    Get album statistics from Gumnut.
    Since Gumnut doesn't support shared albums, all albums are considered owned and not shared.
    """

    gumnut_albums = client.albums.list()
    albums_list = [a async for a in gumnut_albums]
    total_albums = len(albums_list)

    # Gumnut doesn't support shared albums, so all albums are owned and unshared.
    return AlbumStatisticsResponseDto(
        notShared=total_albums,
        owned=total_albums,
        shared=0,
    )


@router.get("/{id}")
async def get_album_info(
    id: UUID,
    withoutAssets: bool = Query(default=None, alias="withoutAssets"),
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AlbumResponseDto:
    """
    Fetch a specific album from Gumnut and convert to AlbumResponseDto format.
    If withoutAssets is False, also fetch and include the album's assets.
    """

    gumnut_album_id = uuid_to_gumnut_album_id(id)

    # Retrieve the specific album from Gumnut
    gumnut_album = await client.albums.retrieve(gumnut_album_id)

    immich_assets = []
    if not withoutAssets:
        async for gumnut_asset in client.assets.list(album_id=gumnut_album_id):
            try:
                immich_assets.append(
                    convert_gumnut_asset_to_immich(gumnut_asset, current_user)
                )
            except Exception as convert_error:
                logger.warning(
                    f"Warning: Could not convert asset {gumnut_asset}: {convert_error}"
                )

    return convert_gumnut_album_to_immich(
        gumnut_album,
        current_user,
        assets=immich_assets,
        asset_count=gumnut_album.asset_count,
    )


@router.post("", status_code=201)
async def create_album(
    request: CreateAlbumDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AlbumResponseDto:
    """
    Create a new album using the Gumnut SDK.
    Note: albumUsers and assetIds are not supported by the Gumnut SDK.
    """

    gumnut_album = await client.albums.create(
        name=request.albumName or "",
        description=request.description,
        # Note: albumUsers and assetIds are not supported in this adapter
    )

    return convert_gumnut_album_to_immich(gumnut_album, current_user, asset_count=0)


@router.put("/{id}/assets")
async def add_assets_to_album(
    id: UUID,
    request: BulkIdsDto,
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[BulkIdResponseDto]:
    """
    Add assets to an album using the Gumnut SDK.
    Returns a list of results indicating success/failure for each asset.
    """

    gumnut_album_id = uuid_to_gumnut_album_id(id)

    response = []
    for asset_uuid in request.ids:
        asset_uuid_str = str(asset_uuid)
        try:
            gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)
            await client.albums.assets_associations.add(
                gumnut_album_id, asset_ids=[gumnut_asset_id]
            )
            response.append(BulkIdResponseDto(id=asset_uuid_str, success=True))
        except ConflictError:
            response.append(
                BulkIdResponseDto(
                    id=asset_uuid_str, success=False, error=Error1.duplicate
                )
            )
        except APIStatusError as asset_error:
            response.append(
                BulkIdResponseDto(
                    id=asset_uuid_str,
                    success=False,
                    error=classify_bulk_item_error(asset_error, Error1),
                )
            )
        except GumnutError as asset_error:
            response.append(
                BulkIdResponseDto(
                    id=asset_uuid_str, success=False, error=Error1.unknown
                )
            )
            log_bulk_transport_error(
                logger,
                context="add_assets_to_album",
                exc=asset_error,
                extra={"asset_id": asset_uuid_str, "album_id": str(id)},
            )

    return response


@router.patch("/{id}")
async def update_album(
    id: UUID,
    request: UpdateAlbumDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AlbumResponseDto:
    """
    Update an album using the Gumnut SDK.
    Only name and description are supported by the Gumnut SDK.
    """

    gumnut_album_id = uuid_to_gumnut_album_id(id)

    update_params = {}
    if request.albumName is not None:
        update_params["name"] = request.albumName
    if request.description is not None:
        update_params["description"] = request.description

    if update_params:
        # SDK raises NotFoundError on missing album → handled by global handler.
        updated_album = await client.albums.update(gumnut_album_id, **update_params)
    else:
        # No-op update still needs to validate existence.
        updated_album = await client.albums.retrieve(gumnut_album_id)

    return convert_gumnut_album_to_immich(updated_album, current_user, asset_count=0)


@router.delete("/{id}/assets")
async def remove_asset_from_album(
    id: UUID,
    request: BulkIdsDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[BulkIdResponseDto]:
    """
    Remove assets from an album using the Gumnut SDK.
    Returns a list of results indicating success/failure for each asset removal.
    """

    gumnut_album_id = uuid_to_gumnut_album_id(id)

    response = []
    for asset_uuid in request.ids:
        asset_uuid_str = str(asset_uuid)
        try:
            gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)
            await client.albums.assets_associations.remove(
                gumnut_album_id, asset_ids=[gumnut_asset_id]
            )
            response.append(BulkIdResponseDto(id=asset_uuid_str, success=True))
        except APIStatusError as asset_error:
            response.append(
                BulkIdResponseDto(
                    id=asset_uuid_str,
                    success=False,
                    error=classify_bulk_item_error(asset_error, Error1),
                )
            )
        except GumnutError as asset_error:
            response.append(
                BulkIdResponseDto(
                    id=asset_uuid_str, success=False, error=Error1.unknown
                )
            )
            log_bulk_transport_error(
                logger,
                context="remove_asset_from_album",
                exc=asset_error,
                extra={"asset_id": asset_uuid_str, "album_id": str(id)},
            )

    return response


@router.delete("/{id}", status_code=204)
async def delete_album(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Delete an album using the Gumnut SDK.
    """

    # SDK raises NotFoundError on missing album → handled by global handler.
    await client.albums.delete(uuid_to_gumnut_album_id(id))
    return Response(status_code=204)


@router.put("/assets")
async def add_assets_to_albums(
    request: AlbumsAddAssetsDto,
    key: str = Query(default=None, alias="key"),
    slug: str = Query(default=None, alias="slug"),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> AlbumsAddAssetsResponseDto:
    """
    Add assets to multiple albums using the Gumnut SDK.
    Returns a single result indicating overall success/failure.
    """

    gumnut_asset_ids = [
        uuid_to_gumnut_asset_id(asset_uuid) for asset_uuid in request.assetIds
    ]

    successful_operations = 0
    total_operations = len(request.albumIds)
    first_error: BulkIdErrorReason | None = None

    for album_uuid in request.albumIds:
        try:
            await client.albums.assets_associations.add(
                uuid_to_gumnut_album_id(album_uuid), asset_ids=gumnut_asset_ids
            )
            successful_operations += 1
        except ConflictError:
            if first_error is None:
                first_error = BulkIdErrorReason.duplicate
        except APIStatusError as album_error:
            if first_error is None:
                first_error = classify_bulk_item_error(album_error, BulkIdErrorReason)
        except GumnutError as album_error:
            if first_error is None:
                first_error = BulkIdErrorReason.unknown
            log_bulk_transport_error(
                logger,
                context="add_assets_to_albums",
                exc=album_error,
                extra={"album_id": str(album_uuid)},
            )

    if successful_operations == total_operations:
        return AlbumsAddAssetsResponseDto(success=True)
    return AlbumsAddAssetsResponseDto(
        success=False, error=first_error or BulkIdErrorReason.unknown
    )


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
