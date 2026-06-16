"""Unit tests for Users API endpoints."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4
import pytest
import shortuuid

from routers.api.users import get_my_user, update_my_user
from routers.immich_models import (
    UserAdminResponseDto,
    UserAvatarColor,
    UserStatus,
    UserLicense,
    UserUpdateMeDto,
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


class TestUpdateMyUser:
    """Test the update_my_user (PUT /me) endpoint quota mapping."""

    def _mock_gumnut_user(self, storage_limit_bytes=None, storage_used_bytes=None):
        """Build a mock Gumnut user with the given storage field values.

        Both default to None, mirroring what the SDK yields when an older
        Gumnut API omits the storage fields during rollout.
        """
        user = Mock(
            spec=[
                "id",
                "email",
                "first_name",
                "last_name",
                "is_active",
                "created_at",
                "updated_at",
                "storage_limit_bytes",
                "storage_used_bytes",
            ]
        )
        user.id = f"intuser_{shortuuid.encode(uuid4())}"
        user.email = "test@example.com"
        user.first_name = "Test"
        user.last_name = "User"
        user.is_active = True
        user.created_at = datetime.now(timezone.utc)
        user.updated_at = datetime.now(timezone.utc)
        user.storage_limit_bytes = storage_limit_bytes
        user.storage_used_bytes = storage_used_bytes
        return user

    @pytest.mark.anyio
    async def test_update_my_user_maps_quota(self):
        """PUT /me sources quota from the Gumnut storage fields."""
        mock_user = self._mock_gumnut_user(
            storage_limit_bytes=100 * 1000**3,
            storage_used_bytes=5 * 1000**3,
        )

        mock_client = Mock()
        mock_client.users.me = AsyncMock(return_value=mock_user)

        result = await update_my_user(request=UserUpdateMeDto(), client=mock_client)

        assert isinstance(result, UserAdminResponseDto)
        assert result.quotaSizeInBytes == 100 * 1000**3
        assert result.quotaUsageInBytes == 5 * 1000**3

    @pytest.mark.anyio
    async def test_update_my_user_quota_none_is_rollout_safe(self):
        """PUT /me reports no quota (None) when storage fields are None."""
        mock_user = self._mock_gumnut_user()  # storage fields default to None

        mock_client = Mock()
        mock_client.users.me = AsyncMock(return_value=mock_user)

        result = await update_my_user(request=UserUpdateMeDto(), client=mock_client)

        assert result.quotaSizeInBytes is None
        assert result.quotaUsageInBytes is None
