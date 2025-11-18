from datetime import datetime, timezone
from typing import List
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from routers.utils.current_user import get_current_user_id
from routers.immich_models import (
    SessionCreateDto,
    SessionCreateResponseDto,
    SessionResponseDto,
    SessionUpdateDto,
)


router = APIRouter(
    prefix="/api/sessions",
    tags=["sessions"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def get_sessions(
    current_user_id: UUID = Depends(get_current_user_id),
) -> List[SessionResponseDto]:
    """
    Get all sessions
    This is a stub implementation that returns a fake session.
    """
    now = datetime.now(timezone.utc).isoformat()
    return [
        SessionResponseDto(
            createdAt=now,
            current=True,
            deviceOS="Web",
            deviceType="WEB",
            expiresAt=None,
            id=str(current_user_id),
            isPendingSyncReset=False,
            updatedAt=now,
        )
    ]


@router.post("", status_code=201)
async def create_session(session_create: SessionCreateDto) -> SessionCreateResponseDto:
    """
    Create a new session
    This is a stub implementation that returns a fake session response.
    """
    now = datetime.now(timezone.utc).isoformat()
    session_id = str(uuid4())
    return SessionCreateResponseDto(
        createdAt=now,
        current=True,
        deviceOS="Web",
        deviceType="WEB",
        expiresAt=None,
        id=session_id,
        isPendingSyncReset=False,
        token="dummy-session-token-" + session_id[:8],
        updatedAt=now,
    )


@router.delete("", status_code=204)
async def delete_all_sessions():
    """
    Delete all sessions
    This is a stub implementation that does not perform any action.
    """
    return


@router.put("/{id}")
async def update_session(
    session_update: SessionUpdateDto,
    id: UUID,
) -> SessionResponseDto:
    """
    Update a session
    This is a stub implementation that returns a fake session response.
    """
    now = datetime.now(timezone.utc).isoformat()
    return SessionResponseDto(
        createdAt=now,
        current=False,
        deviceOS="Web",
        deviceType="WEB",
        expiresAt=None,
        id=str(id),
        isPendingSyncReset=False,
        updatedAt=now,
    )


@router.delete("/{id}", status_code=204)
async def delete_session(id: UUID):
    """
    Delete a session
    This is a stub implementation that does not perform any action.
    """
    return


@router.post("/{id}/lock", status_code=204)
async def lock_session(id: UUID):
    """
    Lock a session
    This is a stub implementation that does not perform any action.
    """
    return
