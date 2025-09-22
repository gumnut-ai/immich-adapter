from typing import List

from fastapi import APIRouter
from routers.immich_models import (
    AssetDeltaSyncDto,
    AssetDeltaSyncResponseDto,
    AssetFullSyncDto,
    AssetResponseDto,
    SyncAckDeleteDto,
    SyncAckDto,
    SyncAckSetDto,
    SyncStreamDto,
)


router = APIRouter(
    prefix="/api/sync",
    tags=["sync"],
    responses={404: {"description": "Not found"}},
)


@router.get("/ack")
async def get_sync_ack() -> List[SyncAckDto]:
    """
    Get sync acknowledgements.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("/ack", status_code=204)
async def send_sync_ack(request: SyncAckSetDto):
    """
    Send sync acknowledgement.
    This is a stub implementation that does not perform any action.
    """
    return


@router.delete("/ack", status_code=204)
async def delete_sync_ack(request: SyncAckDeleteDto):
    """
    Delete sync acknowledgement.
    This is a stub implementation that does not perform any action.
    """
    return


@router.post("/delta-sync")
async def get_delta_sync(request: AssetDeltaSyncDto) -> AssetDeltaSyncResponseDto:
    """
    Get delta sync data.
    This is a stub implementation that returns empty sync response.
    """
    return AssetDeltaSyncResponseDto(
        deleted=[],
        needsFullSync=False,
        upserted=[],
    )


@router.post("/full-sync")
async def get_full_sync_for_user(request: AssetFullSyncDto) -> List[AssetResponseDto]:
    """
    Get full sync for user.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("/stream")
async def get_sync_stream(request: SyncStreamDto):
    """
    Get sync stream.
    This is a stub implementation that returns an empty response.
    """
    return
