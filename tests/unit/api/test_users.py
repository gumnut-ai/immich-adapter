"""Unit tests for Users API endpoints."""

from datetime import datetime, timezone
from uuid import UUID
import pytest

from routers.api.users import get_my_user
from routers.immich_models import (
    UserAdminResponseDto,
    UserAvatarColor,
    UserStatus,
    UserLicense,
)


class TestGetMyUser:
    """Test the get_my_user endpoint."""

    @pytest.mark.anyio
    async def test_get_my_user_success(self):
        """Test successful user fetch with full user data."""
        # Create a test UUID
        test_uuid = UUID("550e8400-e29b-41d4-a716-446655440000")

        # Create a mock UserAdminResponseDto (simulating what get_current_user_admin would return)
        mock_user_admin = UserAdminResponseDto(
            id=str(test_uuid),
            email="test@example.com",
            name="John Doe",
            isAdmin=True,
            createdAt=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            updatedAt=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            avatarColor=UserAvatarColor.primary,
            profileImagePath="",
            shouldChangePassword=False,
            status=UserStatus.active,
            storageLabel="admin",
            quotaSizeInBytes=1024 * 1024 * 1024 * 100,
            quotaUsageInBytes=1024 * 1024 * 1024,
            deletedAt=None,
            oauthId="",
            profileChangedAt=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            license=UserLicense(
                activatedAt=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                activationKey="test-key",
                licenseKey="test-license",
            ),
        )

        # Call the function with the mock dependency
        result = await get_my_user(user_admin=mock_user_admin)

        # Assert response matches expected format (should just return what was passed in)
        assert isinstance(result, UserAdminResponseDto)
        assert result == mock_user_admin
        # ID should be the UUID string
        assert result.id == str(test_uuid)
        assert result.email == "test@example.com"
        assert result.name == "John Doe"
        assert result.isAdmin is True
        assert result.createdAt == datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert result.updatedAt == datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        # Assert Immich-specific defaults
        assert result.avatarColor == UserAvatarColor.primary
        assert result.profileImagePath == ""
        assert result.shouldChangePassword is False
        assert result.status == UserStatus.active
        assert result.storageLabel == "admin"
        assert result.quotaSizeInBytes == 1024 * 1024 * 1024 * 100
        assert result.quotaUsageInBytes == 1024 * 1024 * 1024

    @pytest.mark.anyio
    async def test_get_my_user_admin(self):
        """Test user fetch for an admin user."""
        # Create a test UUID
        test_uuid = UUID("650e8400-e29b-41d4-a716-446655440001")

        # Create a mock UserAdminResponseDto (simulating what get_current_user_admin would return)
        mock_user_admin = UserAdminResponseDto(
            id=str(test_uuid),
            email="admin@example.com",
            name="Admin User",
            isAdmin=True,
            createdAt=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            updatedAt=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            avatarColor=UserAvatarColor.primary,
            profileImagePath="",
            shouldChangePassword=False,
            status=UserStatus.active,
            storageLabel="admin",
            quotaSizeInBytes=1024 * 1024 * 1024 * 100,
            quotaUsageInBytes=1024 * 1024 * 1024,
            deletedAt=None,
            oauthId="",
            profileChangedAt=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            license=UserLicense(
                activatedAt=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                activationKey="test-key",
                licenseKey="test-license",
            ),
        )

        # Call the function
        result = await get_my_user(user_admin=mock_user_admin)

        # Assert admin status
        assert result.isAdmin is True

    @pytest.mark.anyio
    async def test_get_my_user_inactive(self):
        """Test user fetch for an inactive user."""
        # Create a test UUID
        test_uuid = UUID("750e8400-e29b-41d4-a716-446655440002")

        # Create a mock UserAdminResponseDto for an inactive user
        mock_user_admin = UserAdminResponseDto(
            id=str(test_uuid),
            email="inactive@example.com",
            name="Inactive User",
            isAdmin=True,
            createdAt=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            updatedAt=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            avatarColor=UserAvatarColor.primary,
            profileImagePath="",
            shouldChangePassword=False,
            status=UserStatus.deleted,  # Inactive user
            storageLabel="admin",
            quotaSizeInBytes=1024 * 1024 * 1024 * 100,
            quotaUsageInBytes=1024 * 1024 * 1024,
            deletedAt=None,
            oauthId="",
            profileChangedAt=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            license=UserLicense(
                activatedAt=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                activationKey="test-key",
                licenseKey="test-license",
            ),
        )

        # Call the function with the mock dependency
        result = await get_my_user(user_admin=mock_user_admin)

        # Assert status is deleted for inactive users
        assert result.status == UserStatus.deleted
