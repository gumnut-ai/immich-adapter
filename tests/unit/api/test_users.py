"""Unit tests for Users API endpoints."""

from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID
import pytest
import shortuuid

from routers.api.users import get_my_user
from routers.immich_models import UserAdminResponseDto, UserAvatarColor, UserStatus


class TestGetMyUser:
    """Test the get_my_user endpoint."""

    @pytest.mark.anyio
    async def test_get_my_user_success(self):
        """Test successful user fetch with full user data."""
        # Create a test UUID and encode it as a Gumnut user ID
        test_uuid = UUID("550e8400-e29b-41d4-a716-446655440000")
        gumnut_user_id = f"intuser_{shortuuid.encode(test_uuid)}"

        # Create a mock Gumnut client
        mock_client = Mock()
        mock_user = Mock()
        mock_user.id = gumnut_user_id
        mock_user.email = "test@example.com"
        mock_user.first_name = "John"
        mock_user.last_name = "Doe"
        mock_user.is_active = True
        mock_user.is_superuser = False
        mock_user.is_verified = True
        mock_user.created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_user.updated_at = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        mock_client.users.me.return_value = mock_user

        # Call the function
        result = await get_my_user(client=mock_client)

        # Assert SDK method was called
        mock_client.users.me.assert_called_once()

        # Assert response matches expected format
        assert isinstance(result, UserAdminResponseDto)
        # ID should be converted to UUID string
        assert result.id == str(test_uuid)
        assert result.email == "test@example.com"
        assert result.name == "John Doe"
        assert (
            result.isAdmin is True
        )  # Always True - Immich admin status is not like Gumnut superuser
        assert result.createdAt == datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert result.updatedAt == datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        # Assert Immich-specific defaults
        assert result.avatarColor == UserAvatarColor.primary
        assert result.profileImagePath == ""
        assert result.shouldChangePassword is False
        assert result.status == UserStatus.active
        assert result.storageLabel == "admin"  # Default storage label
        assert result.quotaSizeInBytes == 1024 * 1024 * 1024 * 100  # 100GB default
        assert result.quotaUsageInBytes == 1024 * 1024 * 1024  # 1GB default

    @pytest.mark.anyio
    async def test_get_my_user_admin(self):
        """Test user fetch for an admin user."""
        # Create a test UUID and encode it as a Gumnut user ID
        test_uuid = UUID("650e8400-e29b-41d4-a716-446655440001")
        gumnut_user_id = f"intuser_{shortuuid.encode(test_uuid)}"

        # Create a mock Gumnut client
        mock_client = Mock()
        mock_user = Mock()
        mock_user.id = gumnut_user_id
        mock_user.email = "admin@example.com"
        mock_user.first_name = "Admin"
        mock_user.last_name = "User"
        mock_user.is_active = True
        mock_user.is_superuser = True  # Admin user
        mock_user.is_verified = True
        mock_user.created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_user.updated_at = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        mock_client.users.me.return_value = mock_user

        # Call the function
        result = await get_my_user(client=mock_client)

        # Assert admin status
        assert result.isAdmin is True

    @pytest.mark.anyio
    async def test_get_my_user_inactive(self):
        """Test user fetch for an inactive user."""
        # Create a test UUID and encode it as a Gumnut user ID
        test_uuid = UUID("750e8400-e29b-41d4-a716-446655440002")
        gumnut_user_id = f"intuser_{shortuuid.encode(test_uuid)}"

        # Create a mock Gumnut client
        mock_client = Mock()
        mock_user = Mock()
        mock_user.id = gumnut_user_id
        mock_user.email = "inactive@example.com"
        mock_user.first_name = "Inactive"
        mock_user.last_name = "User"
        mock_user.is_active = False  # Inactive user
        mock_user.is_superuser = False
        mock_user.is_verified = True
        mock_user.created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_user.updated_at = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        mock_client.users.me.return_value = mock_user

        # Call the function
        result = await get_my_user(client=mock_client)

        # Assert status is deleted for inactive users
        assert result.status == UserStatus.deleted

    @pytest.mark.anyio
    async def test_get_my_user_no_email(self):
        """Test user fetch when email is None."""
        # Create a test UUID and encode it as a Gumnut user ID
        test_uuid = UUID("850e8400-e29b-41d4-a716-446655440003")
        gumnut_user_id = f"intuser_{shortuuid.encode(test_uuid)}"

        # Create a mock Gumnut client
        mock_client = Mock()
        mock_user = Mock()
        mock_user.id = gumnut_user_id
        mock_user.email = None  # No email
        mock_user.first_name = "No"
        mock_user.last_name = "Email"
        mock_user.is_active = True
        mock_user.is_superuser = False
        mock_user.is_verified = False
        mock_user.created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_user.updated_at = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        mock_client.users.me.return_value = mock_user

        # Call the function
        result = await get_my_user(client=mock_client)

        # Assert email defaults to empty string
        assert result.email == ""

    @pytest.mark.anyio
    async def test_get_my_user_no_first_name(self):
        """Test user fetch when first_name is None."""
        # Create a test UUID and encode it as a Gumnut user ID
        test_uuid = UUID("950e8400-e29b-41d4-a716-446655440004")
        gumnut_user_id = f"intuser_{shortuuid.encode(test_uuid)}"

        # Create a mock Gumnut client
        mock_client = Mock()
        mock_user = Mock()
        mock_user.id = gumnut_user_id
        mock_user.email = "nofirst@example.com"
        mock_user.first_name = None  # No first name
        mock_user.last_name = "Lastname"
        mock_user.is_active = True
        mock_user.is_superuser = False
        mock_user.is_verified = True
        mock_user.created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_user.updated_at = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        mock_client.users.me.return_value = mock_user

        # Call the function
        result = await get_my_user(client=mock_client)

        # Assert name is just the last name
        assert result.name == "Lastname"

    @pytest.mark.anyio
    async def test_get_my_user_no_last_name(self):
        """Test user fetch when last_name is None."""
        # Create a test UUID and encode it as a Gumnut user ID
        test_uuid = UUID("a50e8400-e29b-41d4-a716-446655440005")
        gumnut_user_id = f"intuser_{shortuuid.encode(test_uuid)}"

        # Create a mock Gumnut client
        mock_client = Mock()
        mock_user = Mock()
        mock_user.id = gumnut_user_id
        mock_user.email = "nolast@example.com"
        mock_user.first_name = "Firstname"
        mock_user.last_name = None  # No last name
        mock_user.is_active = True
        mock_user.is_superuser = False
        mock_user.is_verified = True
        mock_user.created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_user.updated_at = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        mock_client.users.me.return_value = mock_user

        # Call the function
        result = await get_my_user(client=mock_client)

        # Assert name is just the first name
        assert result.name == "Firstname"

    @pytest.mark.anyio
    async def test_get_my_user_no_names(self):
        """Test user fetch when both first_name and last_name are None."""
        # Create a test UUID and encode it as a Gumnut user ID
        test_uuid = UUID("b50e8400-e29b-41d4-a716-446655440006")
        gumnut_user_id = f"intuser_{shortuuid.encode(test_uuid)}"

        # Create a mock Gumnut client
        mock_client = Mock()
        mock_user = Mock()
        mock_user.id = gumnut_user_id
        mock_user.email = "noname@example.com"
        mock_user.first_name = None  # No first name
        mock_user.last_name = None  # No last name
        mock_user.is_active = True
        mock_user.is_superuser = False
        mock_user.is_verified = True
        mock_user.created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_user.updated_at = datetime(2024, 1, 15, 12, 0, 0, 0, tzinfo=timezone.utc)

        mock_client.users.me.return_value = mock_user

        # Call the function
        result = await get_my_user(client=mock_client)

        # Assert name defaults to "User"
        assert result.name == "User"

    @pytest.mark.anyio
    async def test_get_my_user_empty_names(self):
        """Test user fetch when both first_name and last_name are empty strings."""
        # Create a test UUID and encode it as a Gumnut user ID
        test_uuid = UUID("c50e8400-e29b-41d4-a716-446655440007")
        gumnut_user_id = f"intuser_{shortuuid.encode(test_uuid)}"

        # Create a mock Gumnut client
        mock_client = Mock()
        mock_user = Mock()
        mock_user.id = gumnut_user_id
        mock_user.email = "empty@example.com"
        mock_user.first_name = ""  # Empty first name
        mock_user.last_name = ""  # Empty last name
        mock_user.is_active = True
        mock_user.is_superuser = False
        mock_user.is_verified = True
        mock_user.created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_user.updated_at = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        mock_client.users.me.return_value = mock_user

        # Call the function
        result = await get_my_user(client=mock_client)

        # Assert name defaults to "User"
        assert result.name == "User"

    @pytest.mark.anyio
    async def test_get_my_user_sdk_error(self):
        """Test handling of SDK errors."""
        from fastapi import HTTPException

        # Create a mock Gumnut client that raises an exception
        mock_client = Mock()
        mock_client.users.me.side_effect = Exception("Backend connection failed")

        # Call the function and expect an HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await get_my_user(client=mock_client)

        # Assert error was mapped properly
        assert exc_info.value.status_code == 500
        assert "Failed to fetch user details" in str(exc_info.value.detail)
