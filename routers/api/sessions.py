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
from services.session_store import Session, SessionStore, get_session_store

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/sessions",
    tags=["sessions"],
    responses={404: {"description": "Not found"}},
)


def _session_to_response_dto(
    session: Session,
    current_session_id: str,
    expires_at: str | None = None,
) -> SessionResponseDto:
    """
    Convert a Session object to SessionResponseDto.

    Args:
        session: The internal Session object
        current_session_id: The session ID of the current request (for setting current=True)
        expires_at: Optional expiration time as ISO string

    Returns:
        SessionResponseDto for the Immich API response
    """
    return SessionResponseDto(
        id=str(session.immich_id),
        createdAt=session.created_at.isoformat(),
        updatedAt=session.updated_at.isoformat(),
        current=(session.id == current_session_id),
        deviceOS=session.device_os,
        deviceType=session.device_type,
        appVersion=session.app_version if session.app_version else None,
        expiresAt=expires_at,
        isPendingSyncReset=session.is_pending_sync_reset,
    )


def _get_jwt_token(request: Request) -> str:
    """Extract JWT token from request state."""
    jwt_token = getattr(request.state, "jwt_token", None)
    if not jwt_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return jwt_token


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
    jwt_token = _get_jwt_token(request)
    current_session_id = SessionStore.hash_jwt(jwt_token)

    sessions = await session_store.get_by_user(str(current_user_id))

    return [
        _session_to_response_dto(session, current_session_id) for session in sessions
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
    jwt_token = _get_jwt_token(request)
    current_session_id = SessionStore.hash_jwt(jwt_token)

    sessions = await session_store.get_by_user(str(current_user_id))

    for session in sessions:
        if session.id != current_session_id:
            try:
                await session_store.delete_by_id(session.id)
            except Exception as e:
                logger.warning(
                    "Failed to delete session during delete_all_sessions",
                    extra={"session_id": session.id, "error": str(e)},
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
    jwt_token = _get_jwt_token(request)
    current_session_id = SessionStore.hash_jwt(jwt_token)

    session = await session_store.get_by_immich_id(str(current_user_id), id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not found",
        )

    # Update the session if isPendingSyncReset is provided
    if session_update.isPendingSyncReset is not None:
        await session_store.set_pending_sync_reset(
            session.id, session_update.isPendingSyncReset
        )
        # Refresh session data after update
        updated_session = await session_store.get_by_id(session.id)
        if updated_session:
            session = updated_session

    return _session_to_response_dto(session, current_session_id)


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
    _get_jwt_token(request)  # Ensure authenticated

    session = await session_store.get_by_immich_id(str(current_user_id), id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not found",
        )

    await session_store.delete_by_id(session.id)
    return None


@router.post("/{id}/lock", status_code=204)
async def lock_session(id: UUID) -> None:
    """
    Lock a session.

    This removes elevated access to locked assets from the session.
    Currently not supported - returns 204 No Content.
    """
    return None
