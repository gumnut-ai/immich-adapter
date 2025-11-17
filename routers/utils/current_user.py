"""
Dependency injection functions for getting current user information.

This module provides lazy-loaded user data that is cached per-request to avoid
repeated calls to the Gumnut backend.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4
import logging

from fastapi import Depends, Request
from gumnut import Gumnut

from routers.immich_models import (
    UserAdminResponseDto,
    UserAvatarColor,
    UserLicense,
    UserResponseDto,
    UserStatus,
)
from routers.utils.error_mapping import map_gumnut_error
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import safe_uuid_from_user_id

logger = logging.getLogger(__name__)


async def get_current_user_admin(
    request: Request,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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

    # Fetch from Gumnut backend
    try:
        user = client.users.me()
    except Exception as e:
        logger.error(f"Failed to fetch user from Gumnut: {e}")
        raise map_gumnut_error(e, "Failed to fetch user details")

    # Map Gumnut UserResponse to Immich UserAdminResponseDto
    # Combine first_name and last_name into Immich's single "name" field
    # Need to include fall back to "User" as names are not required in Gumnut or from OAuth sources
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip() or "User"

    # Convert Gumnut user ID to UUID
    user_uuid = safe_uuid_from_user_id(user.id)

    user_admin_dto = UserAdminResponseDto(
        id=str(user_uuid),
        email=user.email or "",
        name=full_name,
        isAdmin=True,  # Immich admin status is not like Gumnut superuser, so set to True
        createdAt=user.created_at,
        updatedAt=user.updated_at,
        # Immich-specific fields with sensible defaults
        avatarColor=UserAvatarColor.primary,
        profileImagePath="",
        shouldChangePassword=False,
        status=UserStatus.active if user.is_active else UserStatus.deleted,
        storageLabel="admin",
        quotaSizeInBytes=1024 * 1024 * 1024 * 100,
        quotaUsageInBytes=1024 * 1024 * 1024,
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
