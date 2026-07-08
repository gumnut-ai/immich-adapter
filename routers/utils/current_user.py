"""
Dependency injection functions for getting current user information.

This module provides lazy-loaded user data that is cached per-request to avoid
repeated calls to the Gumnut backend.
"""

from datetime import datetime, timezone
from typing import NamedTuple
from uuid import UUID, uuid4

import sentry_sdk
from fastapi import Depends, Request
from gumnut import AsyncGumnut
from gumnut.types.user_response import UserResponse

from routers.api.constants import STUB_LICENSE_KEY
from routers.immich_models import (
    UserAdminResponseDto,
    UserAvatarColor,
    UserLicense,
    UserResponseDto,
    UserStatus,
)
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import safe_uuid_from_user_id


class ImmichUserQuota(NamedTuple):
    """Immich quota fields mapped from a Gumnut user's storage caps.

    ``size_bytes`` is Immich's ``quotaSizeInBytes`` (the per-user cap, from
    Gumnut ``storage_limit_bytes``); ``usage_bytes`` is Immich's
    ``quotaUsageInBytes`` (derived usage, from Gumnut ``storage_used_bytes``).

    Either field is ``None`` when the value is unknown/unlimited: the user has
    no per-user cap, or an older Gumnut API omitted the field during the rollout
    window. (The SDK's non-validating response construction materializes an
    omitted field as ``None`` rather than raising, so this needs no special
    handling.) Immich treats a ``None`` quota as unlimited.
    """

    size_bytes: int | None
    usage_bytes: int | None


def map_user_quota(user: UserResponse) -> ImmichUserQuota:
    """Map a Gumnut user's storage caps to Immich quota fields.

    See ``ImmichUserQuota`` for the field semantics and what ``None`` means.
    """
    return ImmichUserQuota(
        size_bytes=user.storage_limit_bytes,
        usage_bytes=user.storage_used_bytes,
    )


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

    # Attribute this request to the Gumnut user in Sentry as early as the
    # intuser_* id is known, so the active transaction and any error events
    # group per-user. Set the intuser_* id only, never email/PII — matching how
    # the Gumnut backend tags its own Sentry events. This dependency is cached
    # per request, so set_user runs once when the user is first resolved.
    sentry_sdk.set_user({"id": user.id})

    # Map Gumnut UserResponse to Immich UserAdminResponseDto
    # Combine first_name and last_name into Immich's single "name" field
    # Need to include fall back to "User" as names are not required in Gumnut or from OAuth sources
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip() or "User"

    # Convert Gumnut user ID to UUID
    user_uuid = safe_uuid_from_user_id(user.id)

    # Storage cap (max) and derived usage from the Gumnut API, mapped to Immich quota.
    quota = map_user_quota(user)

    user_admin_dto = UserAdminResponseDto(
        id=user_uuid,
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
        quotaSizeInBytes=quota.size_bytes,
        quotaUsageInBytes=quota.usage_bytes,
        deletedAt=None,
        oauthId="",
        profileChangedAt=user.updated_at,
        license=UserLicense(
            activatedAt=datetime.now(tz=timezone.utc),
            activationKey=str(uuid4()),
            licenseKey=STUB_LICENSE_KEY,
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
    return user_admin.id
