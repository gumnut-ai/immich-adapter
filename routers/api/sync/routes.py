"""
Immich sync endpoints for mobile client synchronization.

This module implements the Immich sync protocol, providing both streaming sync
(for beta timeline mode) and full/delta sync (for legacy timeline mode).

The streaming sync uses the photos-api v2 events endpoint (/api/v2/events) to
fetch lightweight event records, then batch-fetches full entities as needed.
Events are processed in priority order (assets before exif, etc.).
"""

import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from gumnut import Gumnut

from services.checkpoint_store import (
    Checkpoint,
    CheckpointStore,
    get_checkpoint_store,
)
from services.session_store import SessionStore, get_session_store

from routers.immich_models import (
    AssetDeltaSyncDto,
    AssetDeltaSyncResponseDto,
    AssetFullSyncDto,
    AssetResponseDto,
    SyncAckDeleteDto,
    SyncAckDto,
    SyncAckSetDto,
    SyncEntityType,
    SyncStreamDto,
    UserResponseDto,
)
from routers.utils.asset_conversion import convert_gumnut_asset_to_immich
from routers.utils.current_user import get_current_user
from routers.utils.gumnut_client import get_authenticated_gumnut_client

from routers.api.sync.stream import (
    _generate_reset_stream,
    _to_ack_string,
    generate_sync_stream,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/sync",
    tags=["sync"],
    responses={404: {"description": "Not found"}},
)


def _get_session_token(request: Request) -> UUID:
    """
    Extract and validate session token from request state.

    The auth middleware stores the session token in request.state.session_token.
    Sync endpoints require a session (API keys are not allowed).

    Args:
        request: The FastAPI request object

    Returns:
        The session UUID

    Raises:
        HTTPException: If session token is missing or invalid
    """
    session_token = getattr(request.state, "session_token", None)
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session required",
        )
    try:
        return UUID(session_token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid session token",
        )


def _parse_ack(ack: str) -> tuple[SyncEntityType, str] | None:
    """
    Parse an ack string into entity type and cursor.

    Ack format for immich-adapter: "SyncEntityType|cursor|"
    - SyncEntityType: Entity type string (e.g., "AssetV1", "AlbumV1")
    - cursor: Opaque v2 events cursor
    - Trailing pipe for future additions

    Matches immich behavior: only throws for invalid entity types, skips
    malformed acks otherwise.

    Args:
        ack: The ack string to parse

    Returns:
        Tuple of (entity_type, cursor), or None if ack is malformed.

    Raises:
        HTTPException: If entity type is invalid (matches immich behavior)
    """
    parts = ack.split("|")
    if len(parts) < 2:
        logger.warning(
            "Skipping malformed ack (too few parts)",
            extra={"ack": ack},
        )
        return None

    entity_type_str = parts[0]

    # Validate entity type - immich throws BadRequestException for invalid types
    try:
        entity_type = SyncEntityType(entity_type_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ack type: {entity_type_str}",
        )

    cursor = parts[1] if parts[1] else ""

    if not cursor:
        logger.warning(
            "Skipping ack with empty cursor",
            extra={"ack": ack, "entity_type": entity_type_str},
        )
        return None

    return entity_type, cursor


@router.get("/ack")
async def get_sync_ack(
    http_request: Request,
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
) -> List[SyncAckDto]:
    """
    Get sync acknowledgements for the current session.

    Returns all stored checkpoints for the session, each containing:
    - type: The sync entity type (e.g., "AssetV1", "AlbumV1")
    - ack: The ack string in format "SyncEntityType|cursor|"

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)

    checkpoints = await checkpoint_store.get_all(session_uuid)

    ack_dtos = [
        SyncAckDto(
            type=checkpoint.entity_type,
            ack=_to_ack_string(
                checkpoint.entity_type,
                checkpoint.cursor,
            ),
        )
        for checkpoint in checkpoints
        if checkpoint.cursor
    ]

    logger.info(
        f"GET /sync/ack returning {len(ack_dtos)} checkpoints",
        extra={
            "session_id": str(session_uuid),
            "checkpoint_count": len(ack_dtos),
            "types": [dto.type.value for dto in ack_dtos],
        },
    )

    return ack_dtos


@router.post("/ack", status_code=204)
async def send_sync_ack(
    request: SyncAckSetDto,
    http_request: Request,
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
    session_store: SessionStore = Depends(get_session_store),
):
    """
    Acknowledge sync checkpoints.

    Parses each ack string and stores the checkpoint for the session.
    If any ack is for SyncResetV1, resets the session's sync progress
    (clears is_pending_sync_reset flag and deletes all checkpoints).

    Ack format for immich-adapter: "SyncEntityType|cursor|"

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)
    session_token = str(session_uuid)

    # Parse all acks and collect checkpoints to store
    # Value is cursor string
    checkpoints_to_store: dict[SyncEntityType, str] = {}

    for idx, ack in enumerate(request.acks):
        parsed = _parse_ack(ack)
        if parsed is None:
            # Malformed ack - skip it (already logged)
            continue

        entity_type, cursor = parsed

        # Handle SyncResetV1 specially - reset sync progress and return
        if entity_type == SyncEntityType.SyncResetV1:
            # Warn if there are other acks that will be ignored
            remaining_acks = len(request.acks) - idx - 1
            ignored_count = len(checkpoints_to_store) + remaining_acks
            if ignored_count > 0:
                logger.warning(
                    "SyncResetV1 encountered - ignoring other acks",
                    extra={
                        "session_id": session_token,
                        "ignored_checkpoint_count": len(checkpoints_to_store),
                        "ignored_remaining_count": remaining_acks,
                    },
                )
            logger.info(
                "SyncResetV1 acknowledged - resetting sync progress",
                extra={"session_id": session_token},
            )
            # Clear the pending sync reset flag
            await session_store.set_pending_sync_reset(session_token, False)
            # Delete all existing checkpoints
            await checkpoint_store.delete_all(session_uuid)
            # Update session activity
            await session_store.update_activity(session_token)
            return

        # Store checkpoint (last one wins if duplicates)
        checkpoints_to_store[entity_type] = cursor

    # Store all checkpoints atomically
    if checkpoints_to_store:
        await checkpoint_store.set_many(
            session_uuid,
            [
                (entity_type, cursor)
                for entity_type, cursor in checkpoints_to_store.items()
            ],
        )

    # Update session activity timestamp
    await session_store.update_activity(session_token)

    logger.info(
        f"Acknowledged {len(checkpoints_to_store)} checkpoints",
        extra={
            "session_id": session_token,
            "checkpoint_count": len(checkpoints_to_store),
            "types": [et.value for et in checkpoints_to_store.keys()],
        },
    )
    return


@router.delete("/ack", status_code=204)
async def delete_sync_ack(
    request: SyncAckDeleteDto,
    http_request: Request,
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
):
    """
    Delete sync acknowledgements - reset sync state.

    If types is None (not provided), deletes all checkpoints for the session.
    If types contains specific types, deletes only those checkpoint types.
    If types is an empty list, does nothing (no-op)

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)

    if request.types is None:
        # No types specified - delete all checkpoints
        await checkpoint_store.delete_all(session_uuid)
        logger.info(
            "Deleted all checkpoints",
            extra={"session_id": str(session_uuid)},
        )
    elif len(request.types) > 0:
        # Specific types requested - delete those
        await checkpoint_store.delete(session_uuid, request.types)
        logger.info(
            f"Deleted {len(request.types)} checkpoint types",
            extra={
                "session_id": str(session_uuid),
                "types": [t.value for t in request.types],
            },
        )
    # else: empty list - do nothing (no-op)
    return


@router.post("/delta-sync")
async def get_delta_sync(
    request: AssetDeltaSyncDto,
    gumnut_client: Gumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> AssetDeltaSyncResponseDto:
    """
    Get delta sync data - returns assets modified after a specific timestamp.

    Note: This implementation fetches all assets and filters in-memory,
    which is inefficient for large libraries.
    """
    try:
        logger.info(f"Delta sync requested for timestamp: {request.updatedAfter}")

        upserted_assets = []
        page_size = 100
        starting_after_id = None

        # Paginate through all assets using cursor-based pagination
        while True:
            # Fetch a page of assets
            assets_page = gumnut_client.assets.list(
                limit=page_size,
                starting_after_id=starting_after_id,
            )

            # Convert to list to process the page
            page_assets = list(assets_page)
            if not page_assets:
                break

            # Filter and convert assets from this page
            for asset in page_assets:
                if asset.updated_at and asset.updated_at > request.updatedAfter:
                    asset_dto = convert_gumnut_asset_to_immich(asset, current_user)
                    upserted_assets.append(asset_dto)

            # Check if there are more pages
            if not assets_page.has_more:
                break

            # Update cursor for next page
            starting_after_id = page_assets[-1].id

        logger.info(
            f"Delta sync found {len(upserted_assets)} updated assets",
            extra={"asset_count": len(upserted_assets)},
        )

        # Note: Deletion tracking not supported by Gumnut
        deleted_asset_ids = []

        return AssetDeltaSyncResponseDto(
            deleted=deleted_asset_ids,
            needsFullSync=False,
            upserted=upserted_assets,
        )

    except Exception as e:
        logger.error(f"Error during delta sync: {str(e)}", exc_info=True)
        return AssetDeltaSyncResponseDto(
            deleted=[],
            needsFullSync=True,
            upserted=[],
        )


@router.post("/full-sync")
async def get_full_sync_for_user(
    request: AssetFullSyncDto,
    gumnut_client: Gumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> List[AssetResponseDto]:
    """
    Get paginated list of assets for full sync (legacy timeline mode).

    Supports cursor-based pagination using lastId parameter.
    """
    try:
        logger.info(
            "Full sync requested",
            extra={
                "limit": request.limit,
                "lastId": request.lastId,
                "updatedUntil": request.updatedUntil,
                "userId": request.userId,
            },
        )

        assets = []
        skip_until_cursor = request.lastId is not None

        for asset in gumnut_client.assets.list():
            # Skip until we find the cursor asset
            if skip_until_cursor:
                if asset.id == request.lastId:
                    skip_until_cursor = False
                    continue
                continue

            # Apply updatedUntil filter
            if request.updatedUntil and asset.updated_at:
                if asset.updated_at > request.updatedUntil:
                    continue

            asset_dto = convert_gumnut_asset_to_immich(asset, current_user)
            assets.append(asset_dto)

            if len(assets) >= request.limit:
                break

        logger.info(
            "Full sync completed",
            extra={
                "asset_count": len(assets),
                "limit": request.limit,
                "has_more": len(assets) >= request.limit,
            },
        )

        return assets

    except Exception as e:
        logger.error(f"Error during full sync: {str(e)}", exc_info=True)
        return []


@router.post("/stream")
async def get_sync_stream(
    request: SyncStreamDto,
    http_request: Request,
    gumnut_client: Gumnut = Depends(get_authenticated_gumnut_client),
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
    session_store: SessionStore = Depends(get_session_store),
):
    """
    Get sync stream as JSON Lines (application/jsonlines+json).

    Streams sync events for all requested entity types using the photos-api
    events endpoint. Events are returned in priority order to ensure proper
    entity dependencies (e.g., assets before exif data).

    Uses stored checkpoints to resume sync from last acknowledged position,
    only returning entities updated after the checkpoint timestamp.

    If request.reset is True, clears all checkpoints before streaming (full sync).
    If session has isPendingSyncReset flag, sends SyncResetV1 and ends immediately.
    """
    session_token = getattr(http_request.state, "session_token", None)
    session_uuid: UUID | None = None

    if session_token:
        try:
            session_uuid = UUID(session_token)
        except (ValueError, TypeError):
            # Invalid session token - continue without session features
            pass

    # Check if session has isPendingSyncReset flag set
    # If so, send SyncResetV1 and end immediately (matches immich behavior)
    if session_uuid:
        session = await session_store.get_by_id(str(session_uuid))
        if session and session.is_pending_sync_reset:
            logger.info(
                "Session has isPendingSyncReset flag - sending SyncResetV1",
                extra={"session_id": session_token},
            )
            return StreamingResponse(
                _generate_reset_stream(),
                media_type="application/jsonlines+json",
            )

    # Handle request.reset flag - clear all checkpoints before streaming
    # This triggers a full sync from the beginning
    if request.reset and session_uuid:
        logger.info(
            "request.reset=True - clearing all checkpoints for full sync",
            extra={"session_id": session_token},
        )
        await checkpoint_store.delete_all(session_uuid)

    # Load checkpoints for delta sync (empty dict if no session or no checkpoints)
    checkpoint_map: dict[SyncEntityType, Checkpoint] = {}
    if session_uuid and not request.reset:
        checkpoints = await checkpoint_store.get_all(session_uuid)
        checkpoint_map = {cp.entity_type: cp for cp in checkpoints}
        logger.debug(
            f"Loaded {len(checkpoint_map)} checkpoints for sync stream",
            extra={
                "session_id": session_token,
                "checkpoint_types": [t.value for t in checkpoint_map.keys()],
            },
        )

    return StreamingResponse(
        generate_sync_stream(gumnut_client, request, checkpoint_map),
        media_type="application/jsonlines+json",
    )
