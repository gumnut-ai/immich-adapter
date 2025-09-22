from fastapi import APIRouter
from uuid import UUID
from typing import List
from datetime import datetime, timezone

from routers.immich_models import (
    TagBulkAssetsResponseDto,
    TagResponseDto,
    TagCreateDto,
    TagUpdateDto,
    TagBulkAssetsDto,
    TagUpsertDto,
    BulkIdsDto,
    BulkIdResponseDto,
)

router = APIRouter(
    prefix="/api/tags",
    tags=["tags"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def get_all_tags() -> List[TagResponseDto]:
    """
    Get all tags.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("", status_code=201)
async def create_tag(request: TagCreateDto) -> TagResponseDto:
    """
    Create a tag.
    This is a stub implementation that returns a fake tag response.
    """
    return TagResponseDto(
        id="tag-id",
        name=request.name,
        value=request.name.lower().replace(" ", "-"),
        color=request.color,
        parentId=str(request.parentId) if request.parentId else None,
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.put("")
async def upsert_tags(request: TagUpsertDto) -> List[TagResponseDto]:
    """
    Upsert tags.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.get("/{id}")
async def get_tag_by_id(id: UUID) -> TagResponseDto:
    """
    Get tag by ID.
    This is a stub implementation that returns a fake tag response.
    """
    return TagResponseDto(
        id=str(id),
        name="Sample Tag",
        value="sample-tag",
        color="#ff0000",
        parentId=None,
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.put("/{id}")
async def update_tag(id: UUID, request: TagUpdateDto) -> TagResponseDto:
    """
    Update tag.
    This is a stub implementation that returns a fake tag response.
    """
    return TagResponseDto(
        id=str(id),
        name="Updated Tag",
        value="updated-tag",
        color=request.color,
        parentId=None,
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.delete("/{id}", status_code=204)
async def delete_tag(id: UUID):
    """
    Delete tag.
    This is a stub implementation that does not perform any action.
    """
    return


@router.put("/{id}/assets")
async def tag_assets(id: UUID, request: BulkIdsDto) -> List[BulkIdResponseDto]:
    """
    Bulk assign assets to tag.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.delete("/{id}/assets")
async def untag_assets(id: UUID, request: BulkIdsDto) -> List[BulkIdResponseDto]:
    """
    Bulk remove assets from tag.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.put("/assets")
async def bulk_tag_assets(request: TagBulkAssetsDto) -> TagBulkAssetsResponseDto:
    """
    Tag assets.
    This is a stub implementation that returns an empty list.
    """
    return TagBulkAssetsResponseDto(count=0)
