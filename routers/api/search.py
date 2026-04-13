from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from uuid import UUID
from datetime import datetime
from gumnut import AsyncGumnut

from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.current_user import get_current_user
from routers.utils.error_mapping import check_for_error_by_code, map_gumnut_error
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_person_id
from routers.utils.person_conversion import convert_gumnut_person_to_immich
from routers.immich_models import (
    PersonResponseDto,
    SearchAlbumResponseDto,
    SearchExploreResponseDto,
    AssetResponseDto,
    AssetTypeEnum,
    AssetVisibility,
    SearchResponseDto,
    SearchStatisticsResponseDto,
    SearchAssetResponseDto,
    MetadataSearchDto,
    SearchSuggestionType,
    SmartSearchDto,
    RandomSearchDto,
    PlacesResponseDto,
    StatisticsSearchDto,
    UserResponseDto,
)
from routers.utils.asset_conversion import convert_gumnut_asset_to_immich

router = APIRouter(
    prefix="/api/search",
    tags=["search"],
    responses={404: {"description": "Not found"}},
)


@router.get("/explore")
async def get_explore_data(
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[SearchExploreResponseDto]:
    """
    Return a list of map markers.
    This is a stub implementation that returns an empty list.
    """

    return []


@router.post("/large-assets")
async def search_large_assets(
    albumIds: list[UUID] = Query(default=None),
    city: str = Query(default=None, nullable=True),
    country: str = Query(default=None, nullable=True),
    createdAfter: datetime = Query(default=None),
    createdBefore: datetime = Query(default=None),
    deviceId: str = Query(default=None),
    isEncoded: bool = Query(default=None),
    isFavorite: bool = Query(default=None),
    isMotion: bool = Query(default=None),
    isNotInAlbum: bool = Query(default=None),
    isOffline: bool = Query(default=None),
    lensModel: str = Query(default=None, nullable=True),
    libraryId: UUID = Query(default=None, nullable=True),
    make: str = Query(default=None),
    minFileSize: int = Query(default=None, ge=0),
    model: str = Query(default=None, nullable=True),
    personIds: list[UUID] = Query(default=None),
    rating: int = Query(default=None, ge=-1, le=5, type="number"),
    size: int = Query(default=None, ge=1, le=1000, type="number"),
    state: str = Query(default=None, nullable=True),
    tagIds: list[UUID] = Query(default=None, nullable=True),
    takenAfter: datetime = Query(default=None),
    takenBefore: datetime = Query(default=None),
    trashedAfter: datetime = Query(default=None),
    trashedBefore: datetime = Query(default=None),
    type: AssetTypeEnum = Query(default=None),
    updatedAfter: datetime = Query(default=None),
    updatedBefore: datetime = Query(default=None),
    visibility: AssetVisibility = Query(default=None),
    withDeleted: bool = Query(default=None),
    withExif: bool = Query(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[AssetResponseDto]:
    """
    Search for large assets based on minimum file size.
    This is a stub implementation as Gumnut does not currently track file size.
    Returns an empty list.
    """

    return []


@router.get("/person")
async def search_person(
    name: str,
    withHidden: bool = Query(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[PersonResponseDto]:
    """Search for people by name."""
    try:
        people = [p async for p in client.people.list(name=name)]
        if withHidden is False:
            people = [p for p in people if not p.is_hidden]
        return [convert_gumnut_person_to_immich(p) for p in people]
    except Exception as e:
        raise map_gumnut_error(e, "Failed to search people") from e


@router.get("/places")
async def search_places(
    name: str = Query(),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[PlacesResponseDto]:
    """
    Search for places by name.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.get("/suggestions")
async def get_search_suggestions(
    type: SearchSuggestionType,
    country: str = Query(default=None),
    includeNull: bool = Query(default=None),
    make: str = Query(default=None),
    model: str = Query(default=None),
    state: str = Query(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[str]:
    """
    Get search suggestions.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("/statistics")
async def search_asset_statistics(
    request: StatisticsSearchDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> SearchStatisticsResponseDto:
    """Get asset count statistics."""
    try:
        from routers.api.timeline import _fetch_asset_counts

        buckets = await _fetch_asset_counts(client)
        total = sum(bucket.count for bucket in buckets)
        return SearchStatisticsResponseDto(total=total)
    except Exception as e:
        raise map_gumnut_error(e, "Failed to get search statistics") from e


@router.post("/metadata")
async def search_assets(
    request: MetadataSearchDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> SearchResponseDto:
    """Search for assets by metadata filters."""
    try:
        person_ids = None
        if request.personIds:
            person_ids = [uuid_to_gumnut_person_id(pid) for pid in request.personIds]

        limit = int(request.size) if request.size else 50
        page = int(request.page) if request.page else 1

        gumnut_results = await client.search.search(
            query=request.description,
            captured_after=request.takenAfter,
            captured_before=request.takenBefore,
            person_ids=person_ids,
            limit=limit,
            page=page,
        )

        immich_assets = []
        if gumnut_results and gumnut_results.data:
            for item in gumnut_results.data:
                immich_assets.append(
                    convert_gumnut_asset_to_immich(item.asset, current_user)
                )

        return SearchResponseDto(
            albums=SearchAlbumResponseDto(count=0, facets=[], items=[], total=0),
            assets=SearchAssetResponseDto(
                count=len(immich_assets),
                facets=[],
                items=immich_assets,
                nextPage="",
                total=len(immich_assets),
            ),
        )
    except Exception as e:
        raise map_gumnut_error(e, "Failed to search assets by metadata") from e


@router.post("/smart")
async def search_smart(
    request: SmartSearchDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> SearchResponseDto:
    """
    Smart search for assets.
    This is a stub implementation that returns empty results.
    """
    try:
        gumnut_assets = await client.search.search(query=request.query)

        # Convert Gumnut assets to Immich format
        immich_assets = []

        if gumnut_assets:
            for item in gumnut_assets.data:
                # Convert Gumnut asset to AssetResponseDto format using utility function
                immich_asset = convert_gumnut_asset_to_immich(item.asset, current_user)
                immich_assets.append(immich_asset)

        return SearchResponseDto(
            albums=SearchAlbumResponseDto(count=0, facets=[], items=[], total=0),
            assets=SearchAssetResponseDto(
                count=len(immich_assets),
                facets=[],
                items=immich_assets,
                nextPage="",
                total=len(immich_assets),
            ),
        )

    except Exception as e:
        # Provide more detailed error information
        error_msg = str(e)
        if check_for_error_by_code(e, 401) or "Invalid API key" in error_msg:
            raise HTTPException(status_code=401, detail="Invalid Gumnut API key")
        elif check_for_error_by_code(e, 403):
            raise HTTPException(status_code=403, detail="Access denied to Gumnut API")
        elif check_for_error_by_code(e, 404):
            raise HTTPException(
                status_code=404, detail="Gumnut albums endpoint not found"
            )
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to fetch albums: {error_msg}"
            )


@router.get("/cities")
async def get_assets_by_city(
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[AssetResponseDto]:
    """
    Get cities for search.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("/random")
async def search_random(
    request: RandomSearchDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[AssetResponseDto]:
    """
    Get random assets.
    This is a stub implementation that returns an empty list.
    """
    return []
