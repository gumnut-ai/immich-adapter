from fastapi import APIRouter
from routers.immich_models import (
    BulkIdsDto,
    TrashResponseDto,
)


router = APIRouter(
    prefix="/api/trash",
    tags=["trash"],
    responses={404: {"description": "Not found"}},
)


@router.post("/empty")
async def empty_trash() -> TrashResponseDto:
    """
    Empty the trash.
    This is a stub implementation that returns zero count.
    """
    return TrashResponseDto(count=0)


@router.post("/restore")
async def restore_trash() -> TrashResponseDto:
    """
    Restore all trashed assets.
    This is a stub implementation that returns zero count.
    """
    return TrashResponseDto(count=0)


@router.post("/restore/assets")
async def restore_assets(request: BulkIdsDto) -> TrashResponseDto:
    """
    Restore specific assets from trash.
    This is a stub implementation that returns zero count.
    """
    return TrashResponseDto(count=0)
