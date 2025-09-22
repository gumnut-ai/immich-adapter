from fastapi import APIRouter, Query
from uuid import UUID
from typing import List

from routers.immich_models import (
    StackResponseDto,
    StackCreateDto,
    StackUpdateDto,
    BulkIdsDto,
)

router = APIRouter(
    prefix="/api/stacks",
    tags=["stacks"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def search_stacks(
    primaryAssetId: UUID = Query(default=None),
) -> List[StackResponseDto]:
    """
    Search stacks.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.delete("", status_code=204)
async def delete_stacks(request: BulkIdsDto):
    """
    Delete multiple stacks.
    This is a stub implementation that does not perform any action.
    """
    return


@router.post("", status_code=201)
async def create_stack(request: StackCreateDto) -> StackResponseDto:
    """
    Create a stack.
    This is a stub implementation that returns a fake stack response.
    """
    return StackResponseDto(
        id="stack-id", primaryAssetId=str(request.assetIds[0]), assets=[]
    )


@router.get("/{id}")
async def get_stack(id: UUID) -> StackResponseDto:
    """
    Get stack by ID.
    This is a stub implementation that returns a fake stack response.
    """
    return StackResponseDto(id=str(id), primaryAssetId="primary-asset-id", assets=[])


@router.put("/{id}")
async def update_stack(id: UUID, request: StackUpdateDto) -> StackResponseDto:
    """
    Update stack.
    This is a stub implementation that returns a fake stack response.
    """
    return StackResponseDto(
        id=str(id),
        primaryAssetId=str(request.primaryAssetId)
        if request.primaryAssetId
        else "primary-asset-id",
        assets=[],
    )


@router.delete("/{id}", status_code=204)
async def delete_stack(id: UUID):
    """
    Delete stack.
    This is a stub implementation that does not perform any action.
    """
    return


@router.delete("/{id}/assets/{assetId}", status_code=204)
async def remove_asset_from_stack(id: UUID, assetId: UUID):
    """
    Remove asset from stack.
    This is a stub implementation that does not perform any action.
    """
    return
