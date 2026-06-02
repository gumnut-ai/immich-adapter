"""
Dependency injection functions for getting current user information.

This module provides lazy-loaded user data that is cached per-request to avoid
repeated calls to the Gumnut backend.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import Depends, Request
from gumnut import AsyncGumnut
from gumnut.types.user_response import UserResponse

from routers.immich_models import (
    UserAdminResponseDto,
    UserAvatarColor,
    UserLicense,
    UserResponseDto,
    UserStatus,
)
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import safe_uuid_from_user_id


def map_user_quota(user: UserResponse) -> tuple[int | None, int | None]:
    """Map Gumnut storage fields to Immich quota fields.

    Returns ``(quotaSizeInBytes, quotaUsageInBytes)`` sourced from the user's
    ``storage_limit_bytes`` (per-user cap) and ``storage_used_bytes`` (derived
    per-user usage) in photos-api's storage caps.

    Rollout-safe with no special handling: if an older photos-api omits these
    fields, the SDK's non-validating response construction materializes them as
    ``None`` (not an error), and Immich treats a ``None`` quota as unlimited /
    unknown. The return type is widened to ``int | None`` to reflect that
    possible-``None`` runtime value during the rollout window.
    """
    return (user.storage_limit_bytes, user.storage_used_bytes)


async def get_current_user_admin(
    request: Request,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> UserAdminResponseDto:
    """
    Get the current user as UserAdminResponseDto.

    This is a lazy dependency - it only calls the backend when first used.
    Results are cached in request.state for the duration of the request.

    Returns:
        UserAdminResponseDto object with full user details
    """
    # Check if we've already fetched it for this request
    if hasattr(request.state, "current_user_admin"):
        return request.state.current_user_admin

    # Fetch from Gumnut backend (SDK errors bubble to the global GumnutError handler)
    user = await client.users.me()

    # Map Gumnut UserResponse to Immich UserAdminResponseDto
    # Combine first_name and last_name into Immich's single "name" field
    # Need to include fall back to "User" as names are not required in Gumnut or from OAuth sources
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip() or "User"

    # Convert Gumnut user ID to UUID
    user_uuid = safe_uuid_from_user_id(user.id)

    # Storage cap (max) and derived usage from photos-api, mapped to Immich quota.
    quota_size, quota_usage = map_user_quota(user)

    user_admin_dto = UserAdminResponseDto(
        id=str(user_uuid),
        email=user.email or "",
        name=full_name,
        isAdmin=False,  # Always False - no need to show Immich admin controls
        createdAt=user.created_at,
        updatedAt=user.updated_at,
        # Immich-specific fields with sensible defaults
        avatarColor=UserAvatarColor.primary,
        profileImagePath="",
        shouldChangePassword=False,
        status=UserStatus.active if user.is_active else UserStatus.deleted,
        storageLabel="admin",
        quotaSizeInBytes=quota_size,
        quotaUsageInBytes=quota_usage,
        deletedAt=None,
        oauthId="",
        profileChangedAt=user.updated_at,
        license=UserLicense(
            activatedAt=datetime.now(tz=timezone.utc),
            activationKey=str(uuid4()),
            licenseKey="/IMSV-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA/",
        ),
    )

    # Cache in request state
    request.state.current_user_admin = user_admin_dto

    return user_admin_dto


async def get_current_user(
    user_admin: UserAdminResponseDto = Depends(get_current_user_admin),
) -> UserResponseDto:
    """
    Get the current user as UserResponseDto.

    This converts the cached UserAdminResponseDto to UserResponseDto format.
    Used for owner fields in assets and albums.

    Returns:
        UserResponseDto object with basic user details
    """
    return UserResponseDto(
        id=user_admin.id,
        email=user_admin.email,
        name=user_admin.name,
        avatarColor=user_admin.avatarColor,
        profileImagePath=user_admin.profileImagePath,
        profileChangedAt=user_admin.profileChangedAt,
    )


async def get_current_user_id(
    user_admin: UserAdminResponseDto = Depends(get_current_user_admin),
) -> UUID:
    """
    Get just the current user's UUID.

    This extracts the UUID from the cached user object without making an additional
    backend call.

    Returns:
        UUID of the current user
    """
    return UUID(user_admin.id)
