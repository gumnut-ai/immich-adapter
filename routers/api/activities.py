from datetime import datetime, timezone
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from routers.utils.current_user import get_current_user
from routers.immich_models import (
    ActivityCreateDto,
    ActivityResponseDto,
    ActivityStatisticsResponseDto,
    ReactionLevel,
    ReactionType,
    UserResponseDto,
    UserAvatarColor,
)


router = APIRouter(
    prefix="/api/activities",
    tags=["activities"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def get_activities(
    albumId: UUID,
    assetId: UUID = Query(default=None),
    level: ReactionLevel = Query(default=None),
    type: ReactionType = Query(default=None),
    userId: UUID = Query(default=None),
) -> List[ActivityResponseDto]:
    """
    Get activities based on query parameters.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("", status_code=201)
async def create_activity(
    request: ActivityCreateDto,
    current_user: UserResponseDto = Depends(get_current_user),
) -> ActivityResponseDto:
    """
    Create a new activity.
    This is a stub implementation that returns a fake activity response.
    """
    now = datetime.now(timezone.utc)
    return ActivityResponseDto(
        assetId=str(current_user.id),
        comment="Test activity comment",
        createdAt=now,
        id="activity-id-123",
        type=ReactionType.comment,
        user=UserResponseDto(
            avatarColor=UserAvatarColor.primary,
            email=current_user.email,
            id=str(current_user.id),
            name=current_user.name,
            profileChangedAt=now,
            profileImagePath="",
        ),
    )


@router.get("/statistics")
async def get_activity_statistics(
    albumId: UUID,
    assetId: UUID = Query(default=None),
) -> ActivityStatisticsResponseDto:
    """
    Get activity statistics.
    This is a stub implementation that returns zero counts.
    """
    return ActivityStatisticsResponseDto(
        comments=0,
        likes=0,
    )


@router.delete("/{id}", status_code=204)
async def delete_activity(id: UUID):
    """
    Delete an activity.
    This is a stub implementation that does not perform any action.
    """
    return
