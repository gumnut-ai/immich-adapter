from fastapi import APIRouter, Query
from uuid import UUID
from datetime import datetime, timezone
from typing import List

from routers.immich_models import (
    AlbumsResponse,
    AssetOrder,
    AssetStatsResponseDto,
    AssetVisibility,
    CastResponse,
    DownloadResponse,
    EmailNotificationsResponse,
    FoldersResponse,
    MemoriesResponse,
    NotificationCreateDto,
    NotificationDto,
    NotificationLevel,
    NotificationType,
    PeopleResponse,
    PurchaseResponse,
    RatingsResponse,
    SharedLinksResponse,
    SystemConfigSmtpDto,
    TagsResponse,
    TemplateDto,
    TemplateResponseDto,
    TestEmailResponseDto,
    UserAdminCreateDto,
    UserAdminDeleteDto,
    UserAdminResponseDto,
    UserAdminUpdateDto,
    UserAvatarColor,
    UserLicense,
    UserPreferencesResponseDto,
    UserPreferencesUpdateDto,
    UserStatus,
)


router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    responses={404: {"description": "Not found"}},
)


@router.post("/auth/unlink-all", status_code=204)
async def unlink_all_oauth_accounts_admin():
    """
    Unlink all OAuth accounts.
    This is a stub implementation that does not perform any action.
    """
    return


@router.post("/notifications", status_code=201)
async def create_notification(request: NotificationCreateDto) -> NotificationDto:
    """
    Create a notification.
    This is a stub implementation that returns a fake notification response.
    """
    return NotificationDto(
        id="notification-id",
        title=request.title,
        level=request.level or NotificationLevel.info,
        type=request.type or NotificationType.Custom,
        createdAt=datetime.now(tz=timezone.utc),
        readAt=None,
    )


@router.post("/notifications/templates/{name}")
async def get_notification_template_admin(
    name: str, request: TemplateDto
) -> TemplateResponseDto:
    """
    Get notification template.
    This is a stub implementation that returns a fake template response.
    """
    return TemplateResponseDto(
        html="<html><body>Template content</body></html>",
        name=name,
    )


@router.post("/notifications/test-email")
async def send_test_email_admin(request: SystemConfigSmtpDto) -> TestEmailResponseDto:
    """
    Send test email.
    This is a stub implementation that returns a fake test email response.
    """
    return TestEmailResponseDto(
        messageId="test-email-message-id",
    )


@router.get("/users")
async def search_users_admin(
    id: UUID = Query(default=None),
    withDeleted: bool = Query(default=None),
) -> List[UserAdminResponseDto]:
    """
    Search users.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("/users", status_code=201)
async def create_user_admin(request: UserAdminCreateDto) -> UserAdminResponseDto:
    """
    Create a user.
    This is a stub implementation that returns a fake user response.
    """
    return UserAdminResponseDto(
        id="user-id",
        email=request.email,
        name=request.name,
        isAdmin=request.isAdmin or False,
        shouldChangePassword=True,
        profileImagePath="",
        avatarColor=request.avatarColor or UserAvatarColor.primary,
        oauthId="",
        status=UserStatus.active,
        storageLabel="admin",
        license=UserLicense(
            activatedAt=datetime.now(tz=timezone.utc),
            activationKey="activation-key",
            licenseKey="license-key",
        ),
        profileChangedAt=datetime.now(tz=timezone.utc),
        deletedAt=datetime.now(tz=timezone.utc),
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
        quotaSizeInBytes=1024 * 1024 * 1024,
        quotaUsageInBytes=0,
    )


@router.get("/users/{id}")
async def get_user_admin(id: UUID) -> UserAdminResponseDto:
    """
    Get user by ID.
    This is a stub implementation that returns a fake user response.
    """
    return UserAdminResponseDto(
        id=str(id),
        email="user@example.com",
        name="User Name",
        isAdmin=False,
        shouldChangePassword=False,
        profileImagePath="",
        avatarColor=UserAvatarColor.primary,
        oauthId="",
        status=UserStatus.active,
        storageLabel="user",
        license=UserLicense(
            activatedAt=datetime.now(tz=timezone.utc),
            activationKey="activation-key",
            licenseKey="license-key",
        ),
        profileChangedAt=datetime.now(tz=timezone.utc),
        deletedAt=datetime.now(tz=timezone.utc),
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
        quotaSizeInBytes=1024 * 1024 * 1024,
        quotaUsageInBytes=0,
    )


@router.put("/users/{id}")
async def update_user_admin(
    id: UUID, request: UserAdminUpdateDto
) -> UserAdminResponseDto:
    """
    Update user.
    This is a stub implementation that returns a fake updated user response.
    """
    return UserAdminResponseDto(
        id=str(id),
        email=request.email or "user@example.com",
        name=request.name or "Updated User",
        isAdmin=request.isAdmin or False,
        shouldChangePassword=request.shouldChangePassword or False,
        profileImagePath="",
        avatarColor=request.avatarColor or UserAvatarColor.primary,
        oauthId="",
        status=UserStatus.active,
        storageLabel="user",
        license=UserLicense(
            activatedAt=datetime.now(tz=timezone.utc),
            activationKey="activation-key",
            licenseKey="license-key",
        ),
        profileChangedAt=datetime.now(tz=timezone.utc),
        deletedAt=datetime.now(tz=timezone.utc),
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
        quotaSizeInBytes=request.quotaSizeInBytes or 1024 * 1024 * 1024,
        quotaUsageInBytes=0,
    )


@router.delete("/users/{id}")
async def delete_user_admin(
    id: UUID, request: UserAdminDeleteDto
) -> UserAdminResponseDto:
    """
    Delete user.
    This is a stub implementation that returns a fake user response.
    """
    return UserAdminResponseDto(
        id=str(id),
        email="deleted@example.com",
        name="Deleted User",
        isAdmin=False,
        shouldChangePassword=False,
        profileImagePath="",
        avatarColor=UserAvatarColor.primary,
        oauthId="",
        status=UserStatus.deleted,
        storageLabel="user",
        license=UserLicense(
            activatedAt=datetime.now(tz=timezone.utc),
            activationKey="activation-key",
            licenseKey="license-key",
        ),
        profileChangedAt=datetime.now(tz=timezone.utc),
        deletedAt=datetime.now(tz=timezone.utc),
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
        quotaSizeInBytes=1024 * 1024 * 1024,
        quotaUsageInBytes=0,
    )


@router.get("/users/{id}/preferences")
async def get_user_preferences_admin(id: UUID) -> UserPreferencesResponseDto:
    """
    Get user preferences.
    This is a stub implementation that returns fake preferences.
    """
    return UserPreferencesResponseDto(
        albums=AlbumsResponse(defaultAssetOrder=AssetOrder.desc),
        cast=CastResponse(gCastEnabled=False),
        download=DownloadResponse(archiveSize=0, includeEmbeddedVideos=False),
        emailNotifications=EmailNotificationsResponse(
            albumInvite=False, albumUpdate=False, enabled=False
        ),
        folders=FoldersResponse(enabled=False, sidebarWeb=False),
        memories=MemoriesResponse(duration=7, enabled=False),
        people=PeopleResponse(enabled=False, sidebarWeb=False),
        purchase=PurchaseResponse(hideBuyButtonUntil="", showSupportBadge=False),
        ratings=RatingsResponse(enabled=False),
        sharedLinks=SharedLinksResponse(enabled=False, sidebarWeb=False),
        tags=TagsResponse(enabled=False, sidebarWeb=False),
    )


@router.put("/users/{id}/preferences")
async def update_user_preferences_admin(
    id: UUID, request: UserPreferencesUpdateDto
) -> UserPreferencesResponseDto:
    """
    Update user preferences.
    This is a stub implementation that returns fake updated preferences.
    """
    return UserPreferencesResponseDto(
        albums=AlbumsResponse(defaultAssetOrder=AssetOrder.desc),
        cast=CastResponse(gCastEnabled=False),
        download=DownloadResponse(archiveSize=0, includeEmbeddedVideos=False),
        emailNotifications=EmailNotificationsResponse(
            albumInvite=False, albumUpdate=False, enabled=False
        ),
        folders=FoldersResponse(enabled=False, sidebarWeb=False),
        memories=MemoriesResponse(duration=7, enabled=False),
        people=PeopleResponse(enabled=False, sidebarWeb=False),
        purchase=PurchaseResponse(hideBuyButtonUntil="", showSupportBadge=False),
        ratings=RatingsResponse(enabled=False),
        sharedLinks=SharedLinksResponse(enabled=False, sidebarWeb=False),
        tags=TagsResponse(enabled=False, sidebarWeb=False),
    )


@router.post("/users/{id}/restore")
async def restore_user_admin(id: UUID) -> UserAdminResponseDto:
    """
    Restore user.
    This is a stub implementation that returns a fake restored user response.
    """
    return UserAdminResponseDto(
        id=str(id),
        email="restored@example.com",
        name="Restored User",
        isAdmin=False,
        shouldChangePassword=False,
        profileImagePath="",
        avatarColor=UserAvatarColor.primary,
        oauthId="",
        status=UserStatus.active,
        storageLabel="user",
        license=UserLicense(
            activatedAt=datetime.now(tz=timezone.utc),
            activationKey="activation-key",
            licenseKey="license-key",
        ),
        profileChangedAt=datetime.now(tz=timezone.utc),
        deletedAt=datetime.now(tz=timezone.utc),
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
        quotaSizeInBytes=1024 * 1024 * 1024,
        quotaUsageInBytes=0,
    )


@router.get("/users/{id}/statistics")
async def get_user_statistics_admin(
    id: UUID,
    isFavorite: bool = Query(default=None),
    isTrashed: bool = Query(default=None),
    visibility: AssetVisibility = Query(default=None),
) -> AssetStatsResponseDto:
    """
    Get user statistics.
    This is a stub implementation that returns zero statistics.
    """
    return AssetStatsResponseDto(
        images=0,
        videos=0,
        total=0,
    )
