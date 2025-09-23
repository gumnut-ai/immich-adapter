from typing import List
from fastapi import APIRouter

from routers.immich_models import AssetResponseDto


router = APIRouter(
    prefix="/api/view",
    tags=["view"],
    responses={404: {"description": "Not found"}},
)


@router.get("/folder")
async def get_assets_by_original_path(path: str) -> List[AssetResponseDto]:
    """
    Get all assets by their original folder path.
    """
    return []


@router.get("/folder/unique-paths")
async def get_unique_original_paths() -> List[str]:
    """
    Get a list of unique original folder paths.
    """
    return []
