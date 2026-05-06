"""Trash router — restore-by-ids, restore-all, and empty-trash flows.

Backed by the photos-api trash primitives:

- ``POST /api/assets/restore`` — bulk restore by ids (idempotent on already-live).
- ``DELETE /api/assets`` (bulk body) — bulk permanent delete by ids.
- ``GET /api/assets?state=trashed`` — paginated trashed listing for the
  restore-all and empty-trash flows.

The SDK does not expose typed methods for these endpoints yet, so calls go
through ``AsyncGumnut.post`` / ``.delete`` directly. Errors propagate to the
global ``GumnutError`` handler.
"""

import asyncio
from itertools import batched
from uuid import UUID

from fastapi import APIRouter, Depends
from gumnut import AsyncGumnut

from routers.immich_models import (
    BulkIdsDto,
    TrashResponseDto,
)
from routers.utils.current_user import get_current_user_id
from routers.utils.gumnut_client import (
    BULK_CHUNK_SIZE,
    get_authenticated_gumnut_client,
)
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    uuid_to_gumnut_asset_id,
)
from services.websockets import emit_user_event, WebSocketEvent


router = APIRouter(
    prefix="/api/trash",
    tags=["trash"],
    responses={404: {"description": "Not found"}},
)


@router.post("/empty")
async def empty_trash(
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user_id: UUID = Depends(get_current_user_id),
) -> TrashResponseDto:
    """Permanently delete every trashed asset belonging to the caller.

    Enumerates the caller's trashed ids, then issues bulk
    ``DELETE /api/assets`` calls in chunks. Emits one ``on_asset_delete`` per
    purged id, matching Immich's wire shape (single-id-per-event for permanent
    deletes). The returned count reflects the upfront enumerated id list;
    between enumeration and the chunked deletes a concurrent request could
    transition some ids, so the count can diverge slightly from rows actually
    purged in this call. The bulk DELETE is idempotent on already-purged ids.
    """
    user_id = str(current_user_id)
    trashed_gumnut_ids = await _list_trashed_ids(client)
    for chunk in batched(trashed_gumnut_ids, BULK_CHUNK_SIZE):
        await client.delete(
            "/api/assets",
            body={"ids": list(chunk)},
            cast_to=type(None),
        )
        # `emit_user_event` is fire-and-forget — gather the per-id emits so a
        # 100-item chunk does one publish wave instead of 100 sequential awaits.
        await asyncio.gather(
            *(
                emit_user_event(
                    WebSocketEvent.ASSET_DELETE,
                    user_id,
                    str(safe_uuid_from_asset_id(gumnut_id)),
                )
                for gumnut_id in chunk
            )
        )
    return TrashResponseDto(count=len(trashed_gumnut_ids))


@router.post("/restore")
async def restore_trash(
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user_id: UUID = Depends(get_current_user_id),
) -> TrashResponseDto:
    """Restore every trashed asset belonging to the caller.

    Enumerates the caller's trashed ids, then issues bulk
    ``POST /api/assets/restore`` calls in chunks. Emits a single batched
    ``on_asset_restore`` event per chunk carrying the chunk's id array. The
    returned count reflects the upfront enumerated id list; concurrent
    transitions between enumeration and the chunked restores can make it
    diverge slightly from rows actually restored in this call. The backend's
    restore endpoint is idempotent on already-live ids.
    """
    user_id = str(current_user_id)
    trashed_gumnut_ids = await _list_trashed_ids(client)
    for chunk in batched(trashed_gumnut_ids, BULK_CHUNK_SIZE):
        await client.post(
            "/api/assets/restore",
            body={"ids": list(chunk)},
            cast_to=type(None),
        )
        chunk_uuid_strs = [str(safe_uuid_from_asset_id(gid)) for gid in chunk]
        await emit_user_event(
            WebSocketEvent.ASSET_RESTORE,
            user_id,
            chunk_uuid_strs,
        )
    return TrashResponseDto(count=len(trashed_gumnut_ids))


@router.post("/restore/assets")
async def restore_assets(
    request: BulkIdsDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user_id: UUID = Depends(get_current_user_id),
) -> TrashResponseDto:
    """Restore the caller's trashed assets identified by the given ids.

    Issues bulk ``POST /api/assets/restore`` calls in chunks of
    ``BULK_CHUNK_SIZE``. Emits a single batched ``on_asset_restore`` event
    per chunk. Already-live ids are silently skipped on the backend; the
    returned count therefore reflects the request size, not the number of
    rows that actually transitioned (the backend's restore endpoint returns
    204 with no count).
    """
    if not request.ids:
        return TrashResponseDto(count=0)

    user_id = str(current_user_id)
    for chunk in batched(request.ids, BULK_CHUNK_SIZE):
        gumnut_ids = [uuid_to_gumnut_asset_id(uid) for uid in chunk]
        await client.post(
            "/api/assets/restore",
            body={"ids": gumnut_ids},
            cast_to=type(None),
        )
        chunk_uuid_strs = [str(uid) for uid in chunk]
        await emit_user_event(
            WebSocketEvent.ASSET_RESTORE,
            user_id,
            chunk_uuid_strs,
        )
    return TrashResponseDto(count=len(request.ids))


async def _list_trashed_ids(client: AsyncGumnut) -> list[str]:
    """Collect every trashed asset id for the caller, paginated.

    The async iterator handles cursor pagination internally. We collect ids
    upfront before any mutations so that paging cursors stay stable — once
    we start restoring or purging, the ``state="trashed"`` view shrinks and
    cursor-based resumption becomes ill-defined.
    """
    return [
        asset.id
        async for asset in client.assets.list(state="trashed", limit=BULK_CHUNK_SIZE)
    ]
