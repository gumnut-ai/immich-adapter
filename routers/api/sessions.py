from typing import List
from uuid import UUID
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from routers.immich_models import (
    SessionCreateDto,
    SessionResponseDto,
    SessionUpdateDto,
)
from routers.utils.current_user import get_current_user_id
from services.websockets import emit_event, WebSocketEvent
from services.session_store import Session, SessionStore, get_session_store

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/sessions",
    tags=["sessions"],
    responses={404: {"description": "Not found"}},
)


def _session_to_response_dto(
    session: Session,
    current_session_token: str,
    expires_at: str | None = None,
) -> SessionResponseDto:
    """
    Convert a Session object to SessionResponseDto.

    Args:
        session: The internal Session object
        current_session_token: The session token of the current request (for setting current=True)
        expires_at: Optional expiration time as ISO string

    Returns:
        SessionResponseDto for the Immich API response
    """
    session_id_str = str(session.id)
    return SessionResponseDto(
        id=session_id_str,
        createdAt=session.created_at.isoformat(),
        updatedAt=session.updated_at.isoformat(),
        current=(session_id_str == current_session_token),
        deviceOS=session.device_os,
        deviceType=session.device_type,
        appVersion=session.app_version if session.app_version else None,
        expiresAt=expires_at,
        isPendingSyncReset=session.is_pending_sync_reset,
    )


def _get_session_token(request: Request) -> str:
    """Extract session token from request state."""
    session_token = getattr(request.state, "session_token", None)
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return session_token


@router.get("")
async def get_sessions(
    request: Request,
    current_user_id: UUID = Depends(get_current_user_id),
    session_store: SessionStore = Depends(get_session_store),
) -> List[SessionResponseDto]:
    """
    Get all sessions for the current user.

    Returns a list of all active sessions, with `current=True` for the
    session making this request.
    """
    current_session_token = _get_session_token(request)

    sessions = await session_store.get_by_user(str(current_user_id))

    return [
        _session_to_response_dto(session, current_session_token) for session in sessions
    ]


@router.post("", status_code=204)
async def create_session(session_create: SessionCreateDto) -> None:
    """
    Create a new session.

    This endpoint is used for casting and requires issuing a new token.
    Currently not supported - returns 204 No Content.
    """
    return None


@router.delete("", status_code=204)
async def delete_all_sessions(
    request: Request,
    current_user_id: UUID = Depends(get_current_user_id),
    session_store: SessionStore = Depends(get_session_store),
) -> None:
    """
    Delete all sessions except the current one.

    This logs out all other devices while keeping the current session active.
    """
    current_session_token = _get_session_token(request)

    sessions = await session_store.get_by_user(str(current_user_id))

    for session in sessions:
        session_token = str(session.id)
        if session_token != current_session_token:
            try:
                await session_store.delete_by_id(session_token)
                # Emit WebSocket event to notify the deleted session's client
                try:
                    await emit_event(
                        WebSocketEvent.SESSION_DELETE, session_token, session_token
                    )
                except Exception as ws_error:
                    logger.warning(
                        "Failed to emit WebSocket event after session delete",
                        extra={"session_id": session_token, "error": str(ws_error)},
                    )
            except Exception as e:
                logger.warning(
                    "Failed to delete session during delete_all_sessions",
                    extra={"session_id": session_token, "error": str(e)},
                    exc_info=True,
                )

    return None


@router.put("/{id}")
async def update_session(
    id: UUID,
    session_update: SessionUpdateDto,
    request: Request,
    current_user_id: UUID = Depends(get_current_user_id),
    session_store: SessionStore = Depends(get_session_store),
) -> SessionResponseDto:
    """
    Update a session.

    Currently only supports updating the isPendingSyncReset flag.
    """
    current_session_token = _get_session_token(request)

    session_token = str(id)
    session = await session_store.get_by_id(session_token)

    # Verify session exists and belongs to current user
    if not session or session.user_id != str(current_user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not found",
        )

    # Update the session if isPendingSyncReset is provided
    if session_update.isPendingSyncReset is not None:
        await session_store.set_pending_sync_reset(
            session_token, session_update.isPendingSyncReset
        )
        # Refresh session data after update
        updated_session = await session_store.get_by_id(session_token)
        if updated_session:
            session = updated_session

    return _session_to_response_dto(session, current_session_token)


@router.delete("/{id}", status_code=204)
async def delete_session(
    id: UUID,
    request: Request,
    current_user_id: UUID = Depends(get_current_user_id),
    session_store: SessionStore = Depends(get_session_store),
) -> None:
    """
    Delete a specific session by ID.
    """
    _get_session_token(request)  # Ensure authenticated

    session_token = str(id)
    session = await session_store.get_by_id(session_token)

    # Verify session exists and belongs to current user
    if not session or session.user_id != str(current_user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not found",
        )

    await session_store.delete_by_id(session_token)

    # Emit WebSocket event to notify the deleted session's client
    try:
        await emit_event(WebSocketEvent.SESSION_DELETE, session_token, session_token)
    except Exception as ws_error:
        logger.warning(
            "Failed to emit WebSocket event after session delete",
            extra={"session_id": session_token, "error": str(ws_error)},
        )

    return None


@router.post("/{id}/lock", status_code=204)
async def lock_session(id: UUID) -> None:
    """
    Lock a session.

    This removes elevated access to locked assets from the session.
    Currently not supported - returns 204 No Content.
    """
    return None
