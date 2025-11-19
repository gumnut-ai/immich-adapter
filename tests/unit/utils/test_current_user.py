"""Tests for current_user.py dependency injection functions."""

import pytest
import shortuuid
from unittest.mock import Mock
from uuid import UUID, uuid4
from datetime import datetime, timezone


from routers.utils.current_user import (
    get_current_user_admin,
    get_current_user,
    get_current_user_id,
)
from routers.immich_models import (
    UserAdminResponseDto,
    UserResponseDto,
    UserStatus,
    UserAvatarColor,
    UserLicense,
)


class TestGetCurrentUserAdmin:
    """Test the get_current_user_admin dependency."""

    @pytest.mark.anyio
    async def test_get_current_user_admin_success(self):
        """Test successful user fetch from backend."""
        # Setup - create mock request with empty state object
        mock_request = Mock()
        mock_request.state = type("obj", (object,), {})()

        # Setup - create mock Gumnut client
        mock_client = Mock()
        mock_user = Mock()
        test_uuid = uuid4()
        mock_user.id = f"intuser_{shortuuid.encode(test_uuid)}"
        mock_user.email = "test@example.com"
        mock_user.first_name = "Test"
        mock_user.last_name = "User"
        mock_user.is_active = True
        mock_user.created_at = datetime.now(timezone.utc)
        mock_user.updated_at = datetime.now(timezone.utc)
        mock_client.users.me.return_value = mock_user

        # Execute
        result = await get_current_user_admin(mock_request, mock_client)

        # Assert
        assert isinstance(result, UserAdminResponseDto)
        assert result.id == str(test_uuid)
        assert result.email == "test@example.com"
        assert result.name == "Test User"
        assert result.isAdmin is False
        assert result.status == UserStatus.active
        mock_client.users.me.assert_called_once()

    @pytest.mark.anyio
    async def test_get_current_user_admin_caching(self):
        """Test that user is cached in request.state."""
        # Setup - create mock request with empty state object
        mock_request = Mock()
        mock_request.state = type("obj", (object,), {})()

        # Setup - create mock Gumnut client
        mock_client = Mock()
        mock_user = Mock()
        test_uuid = uuid4()
        mock_user.id = f"intuser_{shortuuid.encode(test_uuid)}"
        mock_user.email = "test@example.com"
        mock_user.first_name = "Test"
        mock_user.last_name = "User"
        mock_user.is_active = True
        mock_user.created_at = datetime.now(timezone.utc)
        mock_user.updated_at = datetime.now(timezone.utc)
        mock_client.users.me.return_value = mock_user

        # Execute - first call
        result1 = await get_current_user_admin(mock_request, mock_client)

        # Execute - second call (should use cached version)
        result2 = await get_current_user_admin(mock_request, mock_client)

        # Assert
        assert result1 == result2
        # Backend should only be called once due to caching
        mock_client.users.me.assert_called_once()
        # Verify cached value exists in request.state
        assert hasattr(mock_request.state, "current_user_admin")
        assert mock_request.state.current_user_admin == result1

    @pytest.mark.anyio
    async def test_get_current_user_admin_no_name(self):
        """Test user fetch when user has no name."""
        # Setup
        mock_request = Mock()
        mock_request.state = type("obj", (object,), {})()

        mock_client = Mock()
        mock_user = Mock()
        test_uuid = uuid4()
        mock_user.id = f"intuser_{shortuuid.encode(test_uuid)}"
        mock_user.email = "test@example.com"
        mock_user.first_name = None
        mock_user.last_name = None
        mock_user.is_active = True
        mock_user.created_at = datetime.now(timezone.utc)
        mock_user.updated_at = datetime.now(timezone.utc)
        mock_client.users.me.return_value = mock_user

        # Execute
        result = await get_current_user_admin(mock_request, mock_client)

        # Assert
        assert result.name == "User"  # Default name when none provided

    @pytest.mark.anyio
    async def test_get_current_user_admin_inactive_user(self):
        """Test user fetch when user is inactive."""
        # Setup
        mock_request = Mock()
        mock_request.state = type("obj", (object,), {})()

        mock_client = Mock()
        mock_user = Mock()
        test_uuid = uuid4()
        mock_user.id = f"intuser_{shortuuid.encode(test_uuid)}"
        mock_user.email = "test@example.com"
        mock_user.first_name = "Test"
        mock_user.last_name = "User"
        mock_user.is_active = False
        mock_user.created_at = datetime.now(timezone.utc)
        mock_user.updated_at = datetime.now(timezone.utc)
        mock_client.users.me.return_value = mock_user

        # Execute
        result = await get_current_user_admin(mock_request, mock_client)

        # Assert
        assert result.status == UserStatus.deleted


class TestGetCurrentUser:
    """Test the get_current_user dependency."""

    @pytest.mark.anyio
    async def test_get_current_user_conversion(self):
        """Test conversion from UserAdminResponseDto to UserResponseDto."""
        # Setup - create a UserAdminResponseDto
        now = datetime.now(timezone.utc)
        user_admin = UserAdminResponseDto(
            id="123e4567-e89b-12d3-a456-426614174000",
            email="test@example.com",
            name="Test User",
            isAdmin=True,
            createdAt=now,
            updatedAt=now,
            avatarColor=UserAvatarColor.primary,
            profileImagePath="/path/to/image.jpg",
            shouldChangePassword=False,
            status=UserStatus.active,
            storageLabel="admin",
            quotaSizeInBytes=1000000,
            quotaUsageInBytes=500000,
            deletedAt=None,
            oauthId="",
            profileChangedAt=now,
            license=UserLicense(
                activatedAt=now,
                activationKey="key123",
                licenseKey="license123",
            ),
        )

        # Execute
        result = await get_current_user(user_admin)

        # Assert
        assert isinstance(result, UserResponseDto)
        assert result.id == user_admin.id
        assert result.email == user_admin.email
        assert result.name == user_admin.name
        assert result.avatarColor == user_admin.avatarColor
        assert result.profileImagePath == user_admin.profileImagePath
        assert result.profileChangedAt == user_admin.profileChangedAt


class TestGetCurrentUserId:
    """Test the get_current_user_id dependency."""

    @pytest.mark.anyio
    async def test_get_current_user_id_extraction(self):
        """Test extraction of UUID from UserAdminResponseDto."""
        # Setup - create a UserAdminResponseDto
        now = datetime.now(timezone.utc)
        test_uuid = "123e4567-e89b-12d3-a456-426614174000"
        user_admin = UserAdminResponseDto(
            id=test_uuid,
            email="test@example.com",
            name="Test User",
            isAdmin=True,
            createdAt=now,
            updatedAt=now,
            avatarColor=UserAvatarColor.primary,
            profileImagePath="",
            shouldChangePassword=False,
            status=UserStatus.active,
            storageLabel="admin",
            quotaSizeInBytes=1000000,
            quotaUsageInBytes=500000,
            deletedAt=None,
            oauthId="",
            profileChangedAt=now,
            license=UserLicense(
                activatedAt=now,
                activationKey="key123",
                licenseKey="license123",
            ),
        )

        # Execute
        result = await get_current_user_id(user_admin)

        # Assert
        assert isinstance(result, UUID)
        assert str(result) == test_uuid
