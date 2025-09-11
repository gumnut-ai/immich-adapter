from fastapi import APIRouter
# , Depends
# from pydantic import BaseModel

# from routers.immich.models import ImmichAlbum, ImmichAsset, build_immich_asset
# from services.search_service import SearchService, get_search_service

router = APIRouter(
    prefix="/api/search",
    tags=["search"],
    responses={404: {"description": "Not found"}},
)


# class SmartSearchRequest(BaseModel):
#     page: int
#     withExif: bool
#     isVisible: bool
#     query: str


# class SmartSearchAlbumResults(BaseModel):
#     total: int
#     count: int
#     items: list[ImmichAlbum]
#     facets: list[str]
#     nextPage: str | None


# class SmartSearchAssetsResults(BaseModel):
#     total: int
#     count: int
#     items: list[ImmichAsset]
#     facets: list[str]
#     nextPage: str | None


# class SmartSearchResponse(BaseModel):
#     albums: SmartSearchAlbumResults
#     assets: SmartSearchAssetsResults


# @router.post("/smart")
# async def smart_search(
#     request: SmartSearchRequest,
#     search_service: SearchService = Depends(get_search_service),
# ):
#     # TODO: Implement pagination and album search
#     # TODO: Only include exif if request.withExif is true
#     results = await search_service.search(request.query, page=request.page)
#     assets = [asset for asset, _ in results]

#     return SmartSearchResponse(
#         albums=SmartSearchAlbumResults(
#             total=0,
#             count=0,
#             items=[],
#             facets=[],
#             nextPage=None,
#         ),
#         assets=SmartSearchAssetsResults(
#             total=len(assets),
#             count=len(assets),
#             items=[build_immich_asset(asset) for asset in assets],
#             facets=[],
#             nextPage=None,
#         ),
#     )
