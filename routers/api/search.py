from typing import List
from fastapi import APIRouter, HTTPException, Query
from uuid import UUID
from datetime import datetime

from routers.utils.gumnut_client import get_gumnut_client
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
)
from routers.utils.asset_conversion import convert_gumnut_asset_to_immich

router = APIRouter(
    prefix="/api/search",
    tags=["search"],
    responses={404: {"description": "Not found"}},
)


fake_search_response = SearchResponseDto(
    albums=SearchAlbumResponseDto(
        count=0,
        facets=[],
        items=[],
        total=0,
    ),
    assets=SearchAssetResponseDto(
        count=0,
        facets=[],
        items=[],
        nextPage="",
        total=0,
    ),
)


@router.get("/explore")
async def get_explore_data() -> List[SearchExploreResponseDto]:
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
) -> List[PersonResponseDto]:
    """
    Return a list of people.
    Gumnut currently does not support searching for people by name, so this is a stub implementation that returns an empty list.
    """

    return []


@router.get("/places")
async def search_places(
    name: str = Query(),
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
) -> List[str]:
    """
    Get search suggestions.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("/statistics")
async def search_asset_statistics(
    request: StatisticsSearchDto,
) -> SearchStatisticsResponseDto:
    """
    Get search statistics.
    This is a stub implementation that returns zero counts.
    """
    return SearchStatisticsResponseDto(total=0)


@router.post("/metadata")
async def search_assets(request: MetadataSearchDto) -> SearchResponseDto:
    """
    Search for assets by metadata.
    This is a stub implementation that returns empty results.
    """
    return fake_search_response


@router.post("/smart")
async def search_smart(request: SmartSearchDto) -> SearchResponseDto:
    """
    Smart search for assets.
    This is a stub implementation that returns empty results.
    """
    try:
        client = get_gumnut_client()
        gumnut_assets = client.search.search(query=request.query)

        # Convert Gumnut assets to Immich format
        immich_assets = []

        if gumnut_assets:
            for item in gumnut_assets.data:
                # Convert Gumnut asset to AssetResponseDto format using utility function
                immich_asset = convert_gumnut_asset_to_immich(item.asset)
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
        if "401" in error_msg or "Invalid API key" in error_msg:
            raise HTTPException(status_code=401, detail="Invalid Gumnut API key")
        elif "403" in error_msg:
            raise HTTPException(status_code=403, detail="Access denied to Gumnut API")
        elif "404" in error_msg:
            raise HTTPException(
                status_code=404, detail="Gumnut albums endpoint not found"
            )
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to fetch albums: {error_msg}"
            )


@router.get("/cities")
async def get_assets_by_city() -> List[AssetResponseDto]:
    """
    Get cities for search.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("/random")
async def search_random(request: RandomSearchDto) -> List[AssetResponseDto]:
    """
    Get random assets.
    This is a stub implementation that returns an empty list.
    """
    return []
