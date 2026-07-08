import logging
from typing import Annotated, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from gumnut import AsyncGumnut

from routers.utils.bulk import (
    BulkChunkError,
    chunked_per_item_bulk,
    classify_bulk_item_call,
)
from routers.utils.concurrency import gather_with_concurrency
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
from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
)
from routers.utils.asset_conversion import ASSET_INCLUDE, convert_gumnut_asset_to_immich
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

    gumnut_albums = client.albums.list(limit=GUMNUT_API_MAX_PAGE_SIZE, **kwargs)

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

    gumnut_albums = client.albums.list(limit=GUMNUT_API_MAX_PAGE_SIZE)
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
        async for gumnut_asset in client.assets.list(
            album_id=gumnut_album_id,
            include=ASSET_INCLUDE,
            limit=GUMNUT_API_MAX_PAGE_SIZE,
        ):
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

    added: set[str] = set()
    duplicate: set[str] = set()
    not_found: set[str] = set()
    errors_by_uuid: dict[UUID, Error1] = {}
    async for outcome in chunked_per_item_bulk(
        request.ids,
        lambda ids: client.albums.assets_associations.add(
            gumnut_album_id, asset_ids=ids
        ),
        log_context="add_assets_to_album",
        log_extra={"album_id": str(id)},
    ):
        if isinstance(outcome, BulkChunkError):
            for asset_uuid in outcome.chunk_uuids:
                errors_by_uuid[asset_uuid] = outcome.error
            continue
        added.update(outcome.response.added_assets)
        duplicate.update(outcome.response.duplicate_assets)
        not_found.update(outcome.response.not_found_assets)

    # The helper converts uuids internally but doesn't surface the mapping;
    # build it once here so the response-assembly loop is pure dict lookups.
    gumnut_id_by_uuid = {u: uuid_to_gumnut_asset_id(u) for u in request.ids}

    results: list[BulkIdResponseDto] = []
    for asset_uuid in request.ids:
        asset_uuid_str = str(asset_uuid)
        gumnut_asset_id = gumnut_id_by_uuid[asset_uuid]
        if asset_uuid in errors_by_uuid:
            results.append(
                BulkIdResponseDto(
                    id=asset_uuid_str,
                    success=False,
                    error=errors_by_uuid[asset_uuid],
                )
            )
        elif gumnut_asset_id in added:
            results.append(BulkIdResponseDto(id=asset_uuid_str, success=True))
        elif gumnut_asset_id in duplicate:
            results.append(
                BulkIdResponseDto(
                    id=asset_uuid_str, success=False, error=Error1.duplicate
                )
            )
        elif gumnut_asset_id in not_found:
            results.append(
                BulkIdResponseDto(
                    id=asset_uuid_str, success=False, error=Error1.not_found
                )
            )
        else:
            # Contract drift: every requested id should land in exactly one of
            # added / duplicate / not_found. Surface as `unknown` + warning
            # rather than silently succeeding.
            logger.warning(
                "Asset missing from add_assets bulk response",
                extra={"album_id": str(id), "asset_id": asset_uuid_str},
            )
            results.append(
                BulkIdResponseDto(
                    id=asset_uuid_str, success=False, error=Error1.unknown
                )
            )
    return results


@router.patch("/{id}")
async def update_album(
    id: UUID,
    request: UpdateAlbumDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AlbumResponseDto:
    """
    Update an album using the Gumnut SDK.
    Supports name, description, and cover (albumThumbnailAssetId).
    """

    gumnut_album_id = uuid_to_gumnut_album_id(id)

    update_params = {}
    if request.albumName is not None:
        update_params["name"] = request.albumName
    if request.description is not None:
        update_params["description"] = request.description
    if request.albumThumbnailAssetId is not None:
        update_params["album_cover_asset_id"] = uuid_to_gumnut_asset_id(
            request.albumThumbnailAssetId
        )

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

    # Upstream silently skips missing assets and 204s on success.
    errors_by_uuid: dict[UUID, Error1] = {}
    async for outcome in chunked_per_item_bulk(
        request.ids,
        lambda ids: client.albums.assets_associations.remove(
            gumnut_album_id, asset_ids=ids
        ),
        log_context="remove_asset_from_album",
        log_extra={"album_id": str(id)},
    ):
        if isinstance(outcome, BulkChunkError):
            for asset_uuid in outcome.chunk_uuids:
                errors_by_uuid[asset_uuid] = outcome.error

    results: list[BulkIdResponseDto] = []
    for asset_uuid in request.ids:
        asset_uuid_str = str(asset_uuid)
        if asset_uuid in errors_by_uuid:
            results.append(
                BulkIdResponseDto(
                    id=asset_uuid_str,
                    success=False,
                    error=errors_by_uuid[asset_uuid],
                )
            )
        else:
            results.append(BulkIdResponseDto(id=asset_uuid_str, success=True))
    return results


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

    per_album_errors: list[BulkIdErrorReason | None] = await gather_with_concurrency(
        [
            _add_assets_to_one_album(client, album_uuid, gumnut_asset_ids)
            for album_uuid in request.albumIds
        ]
    )

    # Walk in input order so the surfaced error is sticky-first-by-input-order,
    # not first-to-complete (which would vary with scheduler timing).
    first_error: BulkIdErrorReason | None = next(
        (err for err in per_album_errors if err is not None), None
    )
    if first_error is None:
        return AlbumsAddAssetsResponseDto(success=True)
    return AlbumsAddAssetsResponseDto(success=False, error=first_error)


async def _add_assets_to_one_album(
    client: AsyncGumnut,
    album_uuid: UUID,
    gumnut_asset_ids: list[str],
) -> BulkIdErrorReason | None:
    """Add assets to one album; return None on success or the mapped error."""
    return await classify_bulk_item_call(
        client.albums.assets_associations.add(
            uuid_to_gumnut_album_id(album_uuid), asset_ids=gumnut_asset_ids
        ),
        error_enum=BulkIdErrorReason,
        log_context="add_assets_to_albums",
        log_extra={"album_id": str(album_uuid)},
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
