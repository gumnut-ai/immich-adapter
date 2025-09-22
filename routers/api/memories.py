from typing import List
from uuid import UUID
from fastapi import APIRouter, Query
from datetime import datetime
from zoneinfo import ZoneInfo

from routers.api.auth import get_current_user_id
from routers.immich_models import (
    BulkIdResponseDto,
    BulkIdsDto,
    MemoryResponseDto,
    MemoryType,
    MemoryCreateDto,
    MemoryUpdateDto,
    MemoryStatisticsResponseDto,
    OnThisDayDto,
)


router = APIRouter(
    prefix="/api/memories",
    tags=["memories"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def search_memories(
    for_param: datetime = Query(default=None, alias="for"),
    isSaved: bool = Query(default=None),
    isTrashed: bool = Query(default=None),
    type: MemoryType = Query(default=None),
) -> List[MemoryResponseDto]:
    """
    Search memories based on query parameters.
    This is a stub implementation that returns an empty list.
    """

    return []


@router.post("", status_code=201)
async def create_memory(request: MemoryCreateDto) -> MemoryResponseDto:
    """
    Create a new memory.
    This is a stub implementation that returns a fake memory response.
    """
    return MemoryResponseDto(
        id="memory-id",
        assets=[],
        createdAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
        data=OnThisDayDto(year=2024),
        isSaved=False,
        memoryAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
        ownerId=str(get_current_user_id()),
        type=MemoryType.on_this_day,
        updatedAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
    )


@router.get("/statistics")
async def memories_statistics(
    for_param: datetime = Query(default=None, alias="for"),
    isSaved: bool = Query(default=None),
    isTrashed: bool = Query(default=None),
    type: MemoryType = Query(default=None),
) -> MemoryStatisticsResponseDto:
    """
    Get memory statistics.
    This is a stub implementation that returns zero total.
    """
    return MemoryStatisticsResponseDto(total=0)


@router.get("/{id}")
async def get_memory(id: UUID) -> MemoryResponseDto:
    """
    Get a memory by ID.
    This is a stub implementation that returns a fake memory response.
    """
    return MemoryResponseDto(
        id=str(id),
        assets=[],
        createdAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
        data=OnThisDayDto(year=2024),
        isSaved=False,
        memoryAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
        ownerId=str(get_current_user_id()),
        type=MemoryType.on_this_day,
        updatedAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
    )


@router.put("/{id}")
async def update_memory(id: UUID, request: MemoryUpdateDto) -> MemoryResponseDto:
    """
    Update a memory.
    This is a stub implementation that returns a fake memory response.
    """
    return MemoryResponseDto(
        id=str(id),
        assets=[],
        createdAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
        data=OnThisDayDto(year=2024),
        isSaved=request.isSaved or False,
        memoryAt=request.memoryAt or datetime.now(tz=ZoneInfo("Etc/UTC")),
        ownerId=str(get_current_user_id()),
        type=MemoryType.on_this_day,
        updatedAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
    )


@router.delete("/{id}", status_code=204)
async def delete_memory(id: UUID):
    """
    Delete a memory.
    This is a stub implementation that does not perform any action.
    """
    return


@router.delete("/{id}/assets")
async def remove_memory_assets(
    id: UUID, request: BulkIdsDto
) -> List[BulkIdResponseDto]:
    """
    Get assets for a memory.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.put("/{id}/assets")
async def add_memory_assets(id: UUID, request: BulkIdsDto) -> List[BulkIdResponseDto]:
    """
    Get assets for a memory.
    This is a stub implementation that returns an empty list.
    """
    return []
