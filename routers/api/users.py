from datetime import datetime, timezone
from fastapi import APIRouter, Response
from uuid import UUID, uuid4
from typing import List

from routers.immich_models import (
    AlbumsResponse,
    AssetOrder,
    CastResponse,
    CreateProfileImageDto,
    CreateProfileImageResponseDto,
    DownloadResponse,
    EmailNotificationsResponse,
    FoldersResponse,
    MemoriesResponse,
    OnboardingResponseDto,
    PeopleResponse,
    PurchaseResponse,
    RatingsResponse,
    SharedLinksResponse,
    TagsResponse,
    UserAdminResponseDto,
    UserAvatarColor,
    UserLicense,
    UserPreferencesResponseDto,
    UserPreferencesUpdateDto,
    UserStatus,
    UserUpdateMeDto,
    UserResponseDto,
    OnboardingDto,
    LicenseResponseDto,
    LicenseKeyDto,
)

router = APIRouter(
    prefix="/api/users",
    tags=["users"],
    responses={404: {"description": "Not found"}},
)


userAdminResponse: UserAdminResponseDto = UserAdminResponseDto(
    avatarColor=UserAvatarColor.primary,
    createdAt=datetime.now(tz=timezone.utc),
    deletedAt=datetime.now(tz=timezone.utc),
    email="ted@immich.test",
    id="d6773835-4b91-4c7d-8667-26bd5daa1a45",
    isAdmin=True,
    license=UserLicense(
        activatedAt=datetime.now(tz=timezone.utc),
        activationKey=str(uuid4()),
        licenseKey="/IMSV-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA/",
    ),
    name="Ted Mao",
    oauthId="",
    profileChangedAt=datetime.now(tz=timezone.utc),
    profileImagePath="",
    quotaSizeInBytes=1024 * 1024 * 1024 * 100,
    quotaUsageInBytes=1024 * 1024 * 1024,
    shouldChangePassword=False,
    status=UserStatus.active,
    storageLabel="admin",
    updatedAt=datetime.now(tz=timezone.utc),
)

userResponse: UserResponseDto = UserResponseDto(
    avatarColor=UserAvatarColor.primary,
    email="ted@immich.test",
    id="d6773835-4b91-4c7d-8667-26bd5daa1a45",
    name="Ted Mao",
    profileChangedAt=datetime.now(tz=timezone.utc),
    profileImagePath="",
)

userPreferencesResponse: UserPreferencesResponseDto = UserPreferencesResponseDto(
    albums=AlbumsResponse(defaultAssetOrder=AssetOrder.desc),
    cast=CastResponse(gCastEnabled=False),
    download=DownloadResponse(archiveSize=0, includeEmbeddedVideos=False),
    emailNotifications=EmailNotificationsResponse(
        albumInvite=False, albumUpdate=False, enabled=False
    ),
    folders=FoldersResponse(enabled=False, sidebarWeb=False),
    memories=MemoriesResponse(enabled=False),
    people=PeopleResponse(enabled=False, sidebarWeb=False),
    purchase=PurchaseResponse(hideBuyButtonUntil="", showSupportBadge=False),
    ratings=RatingsResponse(enabled=False),
    sharedLinks=SharedLinksResponse(enabled=False, sidebarWeb=False),
    tags=TagsResponse(enabled=False, sidebarWeb=False),
)


@router.get("/me")
async def get_my_user() -> UserAdminResponseDto:
    """
    Get current user details.
    This is a stub implementation that returns fake user data.
    """
    return userAdminResponse


@router.put("/me")
async def update_my_user(request: UserUpdateMeDto) -> UserAdminResponseDto:
    """
    Update current user details.
    This is a stub implementation that returns fake updated user data.
    """
    return userAdminResponse


@router.get("/me/license")
async def get_user_license() -> LicenseResponseDto:
    """
    Get user license.
    This is a stub implementation that returns fake license data.
    """
    return LicenseResponseDto(
        licenseKey="/IMSV-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA/",
        activationKey=str(uuid4()),
        activatedAt=datetime(1900, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


@router.put("/me/license")
async def set_user_license(request: LicenseKeyDto) -> LicenseResponseDto:
    """
    Set user license.
    This is a stub implementation that returns fake license data.
    """
    return LicenseResponseDto(
        licenseKey=request.licenseKey,
        activationKey=request.activationKey,
        activatedAt=datetime.now(timezone.utc),
    )


@router.delete("/me/license", status_code=204)
async def delete_user_license():
    """
    Delete user license.
    This is a stub implementation that does not perform any action.
    """
    return


@router.get("/me/onboarding")
async def get_user_onboarding() -> OnboardingResponseDto:
    """
    Get onboarding status.
    This is a stub implementation that returns onboarded status.
    """
    return OnboardingResponseDto(isOnboarded=True)


@router.put("/me/onboarding")
async def set_user_onboarding(request: OnboardingDto) -> OnboardingResponseDto:
    """
    Set onboarding status.
    This is a stub implementation that does not perform any action.
    """
    return OnboardingResponseDto(isOnboarded=True)


@router.delete("/me/onboarding", status_code=204)
async def delete_user_onboarding():
    """
    Delete onboarding status.
    This is a stub implementation that does not perform any action.
    """
    return


@router.get("/me/preferences")
async def get_my_preferences() -> UserPreferencesResponseDto:
    """
    Get current user preferences.
    This is a stub implementation that returns fake preferences.
    """
    return userPreferencesResponse


@router.put("/me/preferences")
async def update_my_preferences(
    request: UserPreferencesUpdateDto,
) -> UserPreferencesResponseDto:
    """
    Update current user preferences.
    This is a stub implementation that returns fake updated preferences.
    """
    return userPreferencesResponse


@router.post(
    "/profile-image",
    status_code=201,
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {"$ref": "#/components/schemas/CreateProfileImageDto"}
                }
            }
        }
    },
)
async def create_profile_image(
    request: CreateProfileImageDto,
) -> CreateProfileImageResponseDto:
    """
    Upload profile image.
    This is a stub implementation that returns fake updated user data.
    """
    return CreateProfileImageResponseDto(
        profileChangedAt=datetime.now(tz=timezone.utc),
        profileImagePath="path/to/new/profile/image.jpg",
        userId="d6773835-4b91-4c7d-8667-26bd5daa1a45",
    )


@router.delete("/profile-image", status_code=204)
async def delete_profile_image():
    """
    Delete profile image.
    This is a stub implementation that does not perform any action.
    """
    return


@router.get("/{id}")
async def get_user(id: UUID) -> UserResponseDto:
    """
    Get user by ID.
    This is a stub implementation that returns fake user data.
    """
    return userResponse


@router.get("/{id}/profile-image")
async def get_user_profile_image(id: UUID):
    """
    Get profile image for user.
    This is a stub implementation that returns a placeholder response.
    """
    return Response(content=b"fake-image-data", media_type="image/jpeg")


@router.get("")
async def search_users() -> List[UserResponseDto]:
    """
    Search users.
    This is a stub implementation that returns an empty list.
    """
    return []
