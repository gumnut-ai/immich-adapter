from typing import List
from uuid import UUID
from fastapi import APIRouter

from routers.immich_models import BulkIdsDto, DuplicateResponseDto


router = APIRouter(
    prefix="/api/duplicates",
    tags=["duplicates"],
    responses={404: {"description": "Not found"}},
)


@router.delete("/{id}", status_code=204)
async def delete_duplicate(id: UUID) -> None:
    """
    Delete a duplicate asset by its ID.
    Gumnut currently does not support finding duplicates, so this is a stub implementation.
    """

    return


@router.get("")
async def get_asset_duplicates() -> List[DuplicateResponseDto]:
    """
    Return a list of duplicate assets.
    Gumnut currently does not support finding duplicates, so this is a stub implementation that returns an empty list.
    """

    return []


@router.delete("", status_code=204)
async def delete_duplicates(request: BulkIdsDto):
    """
    Deletes a list of duplicate assets.
    Gumnut currently does not support finding duplicates, so this is a stub implementation.
    """

    return
