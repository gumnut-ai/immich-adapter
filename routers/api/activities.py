from datetime import datetime, timezone
from typing import List
from uuid import UUID

from fastapi import APIRouter, Query
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
async def create_activity(request: ActivityCreateDto) -> ActivityResponseDto:
    """
    Create a new activity.
    This is a stub implementation that returns a fake activity response.
    """
    now = datetime.now(timezone.utc)
    return ActivityResponseDto(
        assetId="d6773835-4b91-4c7d-8667-26bd5daa1a45",
        comment="Test activity comment",
        createdAt=now,
        id="activity-id-123",
        type=ReactionType.comment,
        user=UserResponseDto(
            avatarColor=UserAvatarColor.primary,
            email="ted@immich.test",
            id="d6773835-4b91-4c7d-8667-26bd5daa1a45",
            name="Ted Mao",
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
