from typing import List
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Query
from uuid import UUID
from datetime import datetime

from routers.immich_models import (
    NotificationDto,
    NotificationLevel,
    NotificationType,
    NotificationUpdateAllDto,
    NotificationDeleteAllDto,
    NotificationUpdateDto,
)


router = APIRouter(
    prefix="/api/notifications",
    tags=["notifications"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def get_notifications(
    id: UUID = Query(default=None),
    level: NotificationLevel = Query(default=None),
    type: NotificationType = Query(default=None),
    unread: bool = Query(default=None),
) -> List[NotificationDto]:
    """
    Return a list of the user's notifications.
    Gumnut currently does not support notifications, so this is a stub implementation that returns an empty list.
    """

    return []


@router.put("", status_code=204)
async def update_notifications(request: NotificationUpdateAllDto) -> None:
    """
    Update multiple notifications (mark as read/unread).
    This is a stub implementation that does not perform any action.
    """
    return


@router.delete("", status_code=204)
async def delete_notifications(request: NotificationDeleteAllDto):
    """
    Delete multiple notifications.
    This is a stub implementation that does not perform any action.
    """
    return


@router.get("/{id}")
async def get_notification(id: UUID) -> NotificationDto:
    """
    Get a specific notification by ID.
    This is a stub implementation that returns a fake notification.
    """

    return NotificationDto(
        id=str(id),
        title="Test Notification",
        description="This is a test notification",
        level=NotificationLevel.info,
        type=NotificationType.SystemMessage,
        createdAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
        readAt=None,
        data=None,
    )


@router.delete("/{id}", status_code=204)
async def delete_notification(id: UUID):
    """
    Delete a specific notification by ID.
    This is a stub implementation that does not perform any action.
    """
    return


@router.put("/{id}")
async def update_notification(
    id: UUID, request: NotificationUpdateDto
) -> NotificationDto:
    """
    Update a specific notification by ID.
    This is a stub implementation that does not perform any action.
    """
    return NotificationDto(
        id=str(id),
        title="Updated Notification",
        description="This is an updated test notification",
        level=NotificationLevel.info,
        type=NotificationType.SystemMessage,
        createdAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
        readAt=None,
        data=None,
    )
