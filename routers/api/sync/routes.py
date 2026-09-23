"""
Immich sync endpoints for mobile client synchronization.

This module implements the Immich sync protocol via streaming sync. The legacy
full/delta sync endpoints were removed in Immich v3 — Sync v2 supersedes them
over the existing /sync/stream.

The streaming sync uses the Gumnut API events endpoint (/api/events) to
fetch lightweight event records, then batch-fetches full entities as needed.
Events are processed in priority order (assets before exif, etc.).
"""

import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from gumnut import AsyncGumnut

from services.checkpoint_store import (
    Checkpoint,
    CheckpointStore,
    get_checkpoint_store,
)
from services.session_store import SessionStore, get_session_store

from routers.immich_models import (
    SyncAckDeleteDto,
    SyncAckDto,
    SyncAckSetDto,
    SyncEntityType,
    SyncStreamDto,
)
from routers.utils.gumnut_client import (
    get_authenticated_gumnut_client,
    get_bound_library_id,
)

from routers.api.sync.events import bind_sync_epoch, to_ack_string
from routers.api.sync.stream import generate_reset_stream, generate_sync_stream

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


async def _current_epoch(session_store: SessionStore, session_token: str) -> int:
    """The session's sync epoch; checkpoints are read and written under it."""
    session = await session_store.get_by_id(session_token)
    return session.sync_epoch if session else 0


def _parse_ack(ack: str) -> tuple[SyncEntityType, str, int | None] | None:
    """
    Parse an ack string into entity type, cursor, and sync epoch.

    Ack format for immich-adapter: "SyncEntityType|cursor|epoch"
    - SyncEntityType: Entity type string (e.g., "AssetV1", "AlbumV1")
    - cursor: Opaque v2 events cursor
    - epoch: The session sync epoch the ack was issued for; empty in acks
      issued before epochs

    Matches immich behavior: only throws for invalid entity types, skips
    malformed acks otherwise.

    Args:
        ack: The ack string to parse

    Returns:
        Tuple of (entity_type, cursor, epoch), or None if ack is malformed.

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

    epoch = parts[2] if len(parts) > 2 else ""
    return (
        entity_type,
        cursor,
        int(epoch) if epoch.isascii() and epoch.isdigit() else None,
    )


@router.get("/ack")
async def get_sync_ack(
    http_request: Request,
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
    session_store: SessionStore = Depends(get_session_store),
) -> List[SyncAckDto]:
    """
    Get sync acknowledgements for the current session.

    Returns the checkpoints of the session's sync epoch, each containing:
    - type: The sync entity type (e.g., "AssetV1", "AlbumV1")
    - ack: The ack string in format "SyncEntityType|cursor|epoch"

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)

    epoch = await _current_epoch(session_store, str(session_uuid))
    bind_sync_epoch(epoch)
    checkpoints = await checkpoint_store.get_all(session_uuid, epoch)

    ack_dtos = [
        SyncAckDto(
            type=checkpoint.entity_type,
            ack=to_ack_string(
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

    Parses each ack string and stores the checkpoint under the session's sync
    epoch. Acks issued for an epoch the session has left are dropped: they
    belong to a library the session no longer syncs. Acks without an epoch
    (issued before epochs) count as the current one. A SyncResetV1 ack records
    that the client reset to the epoch it was issued for.

    Ack format for immich-adapter: "SyncEntityType|cursor|epoch"

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)
    session_token = str(session_uuid)
    epoch = await _current_epoch(session_store, session_token)

    # Parse all acks and collect checkpoints to store
    # Value is cursor string
    checkpoints_to_store: dict[SyncEntityType, str] = {}
    stale_count = 0

    for idx, ack in enumerate(request.acks):
        parsed = _parse_ack(ack)
        if parsed is None:
            # Malformed ack - skip it (already logged)
            continue

        entity_type, cursor, ack_epoch = parsed
        if ack_epoch is None:
            ack_epoch = epoch

        # Handle SyncResetV1 specially - record the reset and return
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
                extra={"session_id": session_token, "epoch": ack_epoch},
            )
            await session_store.acknowledge_sync_reset(session_token, ack_epoch)
            await session_store.update_activity(session_token)
            return

        if ack_epoch != epoch:
            stale_count += 1
            continue

        # Store checkpoint (last one wins if duplicates)
        checkpoints_to_store[entity_type] = cursor

    # Store all checkpoints atomically, unless the epoch moved meanwhile
    if checkpoints_to_store and not await checkpoint_store.set_many(
        session_uuid, epoch, list(checkpoints_to_store.items())
    ):
        stale_count += len(checkpoints_to_store)
        checkpoints_to_store = {}
    if stale_count:
        logger.info(
            "Dropped acks for a sync epoch the session has left",
            extra={"session_id": session_token, "stale_count": stale_count},
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
    session_store: SessionStore = Depends(get_session_store),
):
    """
    Delete sync acknowledgements - reset sync state.

    If types is None (not provided), deletes all checkpoints for the session.
    If types contains specific types, deletes only those checkpoint types.
    If types is an empty list, does nothing (no-op)

    Requires a session token - API keys are not allowed.
    """
    session_uuid = _get_session_token(http_request)
    epoch = await _current_epoch(session_store, str(session_uuid))

    if request.types is None:
        # No types specified - delete all checkpoints
        await checkpoint_store.delete_all(session_uuid, epoch)
        logger.info(
            "Deleted all checkpoints",
            extra={"session_id": str(session_uuid)},
        )
    elif len(request.types) > 0:
        # Specific types requested - delete those
        await checkpoint_store.delete(session_uuid, epoch, request.types)
        logger.info(
            f"Deleted {len(request.types)} checkpoint types",
            extra={
                "session_id": str(session_uuid),
                "types": [t.value for t in request.types],
            },
        )
    # else: empty list - do nothing (no-op)
    return


@router.post("/stream")
async def get_sync_stream(
    request: SyncStreamDto,
    http_request: Request,
    gumnut_client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    checkpoint_store: CheckpointStore = Depends(get_checkpoint_store),
    session_store: SessionStore = Depends(get_session_store),
):
    """
    Get sync stream as JSON Lines (application/jsonlines+json).

    Streams sync events for all requested entity types using the Gumnut API
    events endpoint. Events are returned in priority order to ensure proper
    entity dependencies (e.g., assets before exif data).

    Uses stored checkpoints to resume sync from last acknowledged position,
    only returning entities updated after the checkpoint timestamp.

    If request.reset is True, clears all checkpoints before streaming (full sync).
    If the client must reset (see above), sends SyncResetV1 and ends immediately.
    """
    session_token = getattr(http_request.state, "session_token", None)
    session_uuid: UUID | None = None

    if session_token:
        try:
            session_uuid = UUID(session_token)
        except (ValueError, TypeError):
            # Invalid session token - continue without session features
            pass

    # The stream reads and acks checkpoints of the session's sync epoch. If the
    # client's local copy belongs to an earlier epoch, or the session has moved
    # off the library this request bound, send SyncResetV1 and end immediately
    # (matches immich behavior).
    epoch = 0
    if session_uuid:
        session = await session_store.get_by_id(str(session_uuid))
        if session:
            epoch = session.sync_epoch
            bind_sync_epoch(epoch)
            library_moved = session.library_id != (get_bound_library_id() or "")
            if session.is_pending_sync_reset or library_moved:
                logger.info(
                    "Session needs a sync reset - sending SyncResetV1",
                    extra={
                        "session_id": session_token,
                        "library_moved": library_moved,
                    },
                )
                return StreamingResponse(
                    generate_reset_stream(),
                    media_type="application/jsonlines+json",
                )

    # Handle request.reset flag - clear all checkpoints before streaming
    # This triggers a full sync from the beginning
    if request.reset and session_uuid:
        logger.info(
            "request.reset=True - clearing all checkpoints for full sync",
            extra={"session_id": session_token},
        )
        await checkpoint_store.delete_all(session_uuid, epoch)

    # Load checkpoints for delta sync (empty dict if no session or no checkpoints)
    checkpoint_map: dict[SyncEntityType, Checkpoint] = {}
    if session_uuid and not request.reset:
        checkpoints = await checkpoint_store.get_all(session_uuid, epoch)
        checkpoint_map = {cp.entity_type: cp for cp in checkpoints}
        logger.debug(
            f"Loaded {len(checkpoint_map)} checkpoints for sync stream",
            extra={
                "session_id": session_token,
                "checkpoint_types": [t.value for t in checkpoint_map.keys()],
            },
        )

    # Fetch current user before starting the stream. This ensures auth
    # errors (e.g. expired JWT) return a proper HTTP 401 instead of being
    # silently swallowed inside the streaming generator after the 200
    # status has already been committed. SDK errors bubble to the global
    # GumnutError handler.
    current_user = await gumnut_client.users.me()

    return StreamingResponse(
        generate_sync_stream(gumnut_client, request, checkpoint_map, current_user),
        media_type="application/jsonlines+json",
    )
