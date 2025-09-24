from datetime import datetime, timezone

from fastapi import APIRouter
from routers.immich_models import (
    LoginResponseDto,
    OAuthAuthorizeResponseDto,
    OAuthCallbackDto,
    OAuthConfigDto,
    UserAdminResponseDto,
    UserAvatarColor,
    UserLicense,
    UserStatus,
)


router = APIRouter(
    prefix="/api/oauth",
    tags=["oauth"],
    responses={404: {"description": "Not found"}},
)


@router.post("/authorize", status_code=201)
async def start_oauth(oauth_config: OAuthConfigDto) -> OAuthAuthorizeResponseDto:
    """
    Start OAuth authentication process
    This is a stub implementation that does not perform any action.
    """
    return OAuthAuthorizeResponseDto(
        url="https://oauth.provider.com/authorize?client_id=immich&redirect_uri="
        + oauth_config.redirectUri
    )


@router.post("/callback", status_code=201)
async def finish_oauth(oauth_callback: OAuthCallbackDto) -> LoginResponseDto:
    """
    Finish OAuth authentication process
    This is a stub implementation that returns a fake login response.
    """
    return LoginResponseDto(
        accessToken="y3NP8DRmNE1K2DCNsVZKPepmqIWXQyoghTGS9aDjBM",
        isAdmin=True,
        isOnboarded=True,
        name="Ted Mao",
        profileImagePath="",
        shouldChangePassword=False,
        userEmail="ted@immich.test",
        userId="d6773835-4b91-4c7d-8667-26bd5daa1a45",
    )


@router.post("/link")
async def link_oauth_account(oauth_callback: OAuthCallbackDto) -> UserAdminResponseDto:
    """
    Link OAuth account to existing user
    This is a stub implementation that returns a fake user response.
    """
    now = datetime.now(timezone.utc)
    return UserAdminResponseDto(
        avatarColor=UserAvatarColor.primary,
        createdAt=now,
        deletedAt=now,
        email="ted@immich.test",
        id="d6773835-4b91-4c7d-8667-26bd5daa1a45",
        isAdmin=True,
        license=UserLicense(
            activatedAt=now,
            activationKey="dummy-activation-key",
            licenseKey="dummy-license-key",
        ),
        name="Ted Mao",
        oauthId="oauth-123456",
        profileChangedAt=now,
        profileImagePath="",
        quotaSizeInBytes=0,
        quotaUsageInBytes=0,
        shouldChangePassword=False,
        status=UserStatus.active,
        storageLabel="default",
        updatedAt=now,
    )


@router.get("/mobile-redirect")
async def redirect_oauth_to_mobile():
    """
    Redirect OAuth to mobile application
    This is a stub implementation that does not perform any action.
    """
    return {"message": "Redirecting to mobile app"}


@router.post("/unlink")
async def unlink_oauth_account() -> UserAdminResponseDto:
    """
    Unlink OAuth account from user
    This is a stub implementation that returns a fake user response.
    """
    now = datetime.now(timezone.utc)
    return UserAdminResponseDto(
        avatarColor=UserAvatarColor.primary,
        createdAt=now,
        deletedAt=now,
        email="ted@immich.test",
        id="d6773835-4b91-4c7d-8667-26bd5daa1a45",
        isAdmin=True,
        license=UserLicense(
            activatedAt=now,
            activationKey="dummy-activation-key",
            licenseKey="dummy-license-key",
        ),
        name="Ted Mao",
        oauthId="",
        profileChangedAt=now,
        profileImagePath="",
        quotaSizeInBytes=0,
        quotaUsageInBytes=0,
        shouldChangePassword=False,
        status=UserStatus.active,
        storageLabel="default",
        updatedAt=now,
    )
