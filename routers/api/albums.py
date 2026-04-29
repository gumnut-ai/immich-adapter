import asyncio
import logging
from typing import Annotated, Any, Coroutine, List, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from gumnut import (
    APIStatusError,
    AsyncGumnut,
    ConflictError,
    GumnutError,
    NotFoundError,
)

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

BULK_ASSOCIATION_CONCURRENCY_LIMIT = 10

T = TypeVar("T")


async def _gather_with_concurrency(coros: list[Coroutine[Any, Any, T]]) -> list[T]:
    """Run coroutines in parallel with bounded concurrency.

    Output preserves input order regardless of completion order — relied on by
    callers to keep response ordering. If any coroutine raises, ``gather``
    cancels pending siblings and the exception propagates, so callers must
    catch per-item errors inside the coroutine rather than relying on this
    helper to surface them.
    """
    semaphore = asyncio.Semaphore(BULK_ASSOCIATION_CONCURRENCY_LIMIT)

    async def _run(coro: Coroutine[Any, Any, T]) -> T:
        async with semaphore:
            return await coro

    return await asyncio.gather(*(_run(coro) for coro in coros))


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
    album_id_str = str(id)
    asset_uuids = list(request.ids)
    gumnut_asset_ids = [
        uuid_to_gumnut_asset_id(asset_uuid) for asset_uuid in asset_uuids
    ]

    # The upstream POST /api/albums/{album_id}/assets accepts the full asset_id
    # list and returns added/duplicate split server-side, so the happy path is a
    # single round-trip. The upstream 404s the entire batch when any asset is
    # missing or in a different library — fall back to per-item calls to recover
    # per-asset granularity for the BulkIdResponseDto contract.
    try:
        bulk_response = await client.albums.assets_associations.add(
            gumnut_album_id, asset_ids=gumnut_asset_ids
        )
    except NotFoundError:
        return await _gather_with_concurrency(
            [
                _add_single_asset(client, gumnut_album_id, album_id_str, asset_uuid)
                for asset_uuid in asset_uuids
            ]
        )
    except APIStatusError as bulk_error:
        error = classify_bulk_item_error(bulk_error, Error1)
        return [
            BulkIdResponseDto(id=str(asset_uuid), success=False, error=error)
            for asset_uuid in asset_uuids
        ]
    except GumnutError as bulk_error:
        log_bulk_transport_error(
            logger,
            context="add_assets_to_album",
            exc=bulk_error,
            extra={"album_id": album_id_str, "asset_count": len(asset_uuids)},
        )
        return [
            BulkIdResponseDto(id=str(asset_uuid), success=False, error=Error1.unknown)
            for asset_uuid in asset_uuids
        ]

    added = set(bulk_response.added_assets)
    duplicate = set(bulk_response.duplicate_assets)
    return [
        _classify_add_response_item(
            asset_uuid=asset_uuid,
            gumnut_asset_id=gumnut_asset_id,
            added=added,
            duplicate=duplicate,
            album_id_str=album_id_str,
        )
        for gumnut_asset_id, asset_uuid in zip(gumnut_asset_ids, asset_uuids)
    ]


def _classify_add_response_item(
    *,
    asset_uuid: UUID,
    gumnut_asset_id: str,
    added: set[str],
    duplicate: set[str],
    album_id_str: str,
) -> BulkIdResponseDto:
    """Map a single asset_id against an add-response's added/duplicate sets.

    Used by both the bulk happy path and the per-asset fallback so they classify
    the upstream response identically. An asset_id absent from both sets is
    treated as ``unknown`` (with a warning) rather than silently succeeding —
    shouldn't happen with the current photos-api implementation, but surfacing
    it makes drift visible.
    """
    asset_uuid_str = str(asset_uuid)
    if gumnut_asset_id in added:
        return BulkIdResponseDto(id=asset_uuid_str, success=True)
    if gumnut_asset_id in duplicate:
        return BulkIdResponseDto(
            id=asset_uuid_str, success=False, error=Error1.duplicate
        )
    logger.warning(
        "Asset missing from add_assets bulk response",
        extra={"album_id": album_id_str, "asset_id": asset_uuid_str},
    )
    return BulkIdResponseDto(id=asset_uuid_str, success=False, error=Error1.unknown)


async def _add_single_asset(
    client: AsyncGumnut,
    gumnut_album_id: str,
    album_id_str: str,
    asset_uuid: UUID,
) -> BulkIdResponseDto:
    """Per-asset fallback used when the bulk add 404s on a mixed valid/invalid set."""
    asset_uuid_str = str(asset_uuid)
    try:
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)
        single_response = await client.albums.assets_associations.add(
            gumnut_album_id, asset_ids=[gumnut_asset_id]
        )
    except APIStatusError as asset_error:
        return BulkIdResponseDto(
            id=asset_uuid_str,
            success=False,
            error=classify_bulk_item_error(asset_error, Error1),
        )
    except GumnutError as asset_error:
        log_bulk_transport_error(
            logger,
            context="add_assets_to_album",
            exc=asset_error,
            extra={"asset_id": asset_uuid_str, "album_id": album_id_str},
        )
        return BulkIdResponseDto(id=asset_uuid_str, success=False, error=Error1.unknown)

    return _classify_add_response_item(
        asset_uuid=asset_uuid,
        gumnut_asset_id=gumnut_asset_id,
        added=set(single_response.added_assets),
        duplicate=set(single_response.duplicate_assets),
        album_id_str=album_id_str,
    )


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
    album_id_str = str(id)
    asset_uuids = list(request.ids)
    gumnut_asset_ids = [
        uuid_to_gumnut_asset_id(asset_uuid) for asset_uuid in asset_uuids
    ]

    # The upstream DELETE /api/albums/{album_id}/assets accepts the full
    # asset_id list, silently skips missing IDs, and returns 204. A single
    # round-trip covers all assets — we surface batch-level errors (e.g. the
    # album itself is missing) by mapping the same error onto every entry.
    try:
        await client.albums.assets_associations.remove(
            gumnut_album_id, asset_ids=gumnut_asset_ids
        )
    except APIStatusError as bulk_error:
        error = classify_bulk_item_error(bulk_error, Error1)
        return [
            BulkIdResponseDto(id=str(asset_uuid), success=False, error=error)
            for asset_uuid in asset_uuids
        ]
    except GumnutError as bulk_error:
        log_bulk_transport_error(
            logger,
            context="remove_asset_from_album",
            exc=bulk_error,
            extra={"album_id": album_id_str, "asset_count": len(asset_uuids)},
        )
        return [
            BulkIdResponseDto(id=str(asset_uuid), success=False, error=Error1.unknown)
            for asset_uuid in asset_uuids
        ]

    return [
        BulkIdResponseDto(id=str(asset_uuid), success=True)
        for asset_uuid in asset_uuids
    ]


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

    async def _add_asset_ids_to_album(
        album_uuid: UUID,
    ) -> tuple[bool, BulkIdErrorReason | None]:
        try:
            await client.albums.assets_associations.add(
                uuid_to_gumnut_album_id(album_uuid), asset_ids=gumnut_asset_ids
            )
            return True, None
        except ConflictError:
            return False, BulkIdErrorReason.duplicate
        except APIStatusError as album_error:
            return False, classify_bulk_item_error(album_error, BulkIdErrorReason)
        except GumnutError as album_error:
            log_bulk_transport_error(
                logger,
                context="add_assets_to_albums",
                exc=album_error,
                extra={"album_id": str(album_uuid)},
            )
            return False, BulkIdErrorReason.unknown

    album_results = await _gather_with_concurrency(
        [_add_asset_ids_to_album(album_uuid) for album_uuid in request.albumIds]
    )

    successful_operations = sum(
        1 for operation_success, _ in album_results if operation_success
    )
    total_operations = len(request.albumIds)
    first_error = next(
        (
            operation_error
            for operation_success, operation_error in album_results
            if not operation_success
        ),
        None,
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
