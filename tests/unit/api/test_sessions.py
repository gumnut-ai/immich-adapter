"""Unit tests for Sessions API endpoints."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from routers.api.sessions import (
    _get_jwt_token,
    _session_to_response_dto,
    create_session,
    delete_all_sessions,
    delete_session,
    get_sessions,
    lock_session,
    update_session,
)
from routers.immich_models import SessionCreateDto, SessionUpdateDto
from services.session_store import Session, SessionStore

# Test UUIDs for consistent testing
TEST_IMMICH_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_IMMICH_ID_2 = UUID("650e8400-e29b-41d4-a716-446655440001")


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_session_to_response_dto_current_true(self):
        """Test converting session to DTO when it's the current session."""
        now = datetime.now(timezone.utc)
        session_id = "abc123def456789012345678901234567890123456789012345678901234"
        session = Session(
            id=session_id,
            immich_id=TEST_IMMICH_ID,
            user_id="user_1",
            library_id="lib_1",
            device_type="iOS",
            device_os="iOS 17",
            app_version="1.94.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )

        result = _session_to_response_dto(session, session_id)

        assert result.current is True
        assert result.deviceType == "iOS"
        assert result.deviceOS == "iOS 17"
        assert result.appVersion == "1.94.0"
        assert result.isPendingSyncReset is False
        assert result.id == str(TEST_IMMICH_ID)

    def test_session_to_response_dto_current_false(self):
        """Test converting session to DTO when it's not the current session."""
        now = datetime.now(timezone.utc)
        session = Session(
            id="abc123def456789012345678901234567890123456789012345678901234",
            immich_id=TEST_IMMICH_ID,
            user_id="user_1",
            library_id="lib_1",
            device_type="iOS",
            device_os="iOS 17",
            app_version="1.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=True,
        )

        result = _session_to_response_dto(session, "different_session_id")

        assert result.current is False
        assert result.isPendingSyncReset is True
        assert result.id == str(TEST_IMMICH_ID)

    def test_session_to_response_dto_empty_app_version(self):
        """Test that empty app_version is converted to None."""
        now = datetime.now(timezone.utc)
        session = Session(
            id="abc123def456789012345678901234567890123456789012345678901234",
            immich_id=TEST_IMMICH_ID,
            user_id="user_1",
            library_id="lib_1",
            device_type="Web",
            device_os="Chrome",
            app_version="",  # Empty string
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )

        result = _session_to_response_dto(session, "other")

        assert result.appVersion is None

    def test_get_jwt_token_success(self):
        """Test extracting JWT from request state."""
        mock_request = Mock()
        mock_request.state.jwt_token = "test-jwt-token"

        result = _get_jwt_token(mock_request)

        assert result == "test-jwt-token"

    def test_get_jwt_token_missing_raises_401(self):
        """Test that missing JWT raises 401."""
        mock_request = Mock()
        mock_request.state = Mock(spec=[])  # No jwt_token attribute

        with pytest.raises(HTTPException) as exc_info:
            _get_jwt_token(mock_request)

        assert exc_info.value.status_code == 401
        assert "Authentication required" in exc_info.value.detail


class TestGetSessions:
    """Tests for GET /sessions endpoint."""

    @pytest.fixture
    def mock_session_store(self):
        """Create a mock SessionStore."""
        store = AsyncMock(spec=SessionStore)
        return store

    @pytest.fixture
    def mock_request(self):
        """Create a mock request with JWT token."""
        request = Mock()
        request.state.jwt_token = "test-jwt-token"
        return request

    @pytest.fixture
    def sample_sessions(self):
        """Create sample sessions for testing."""
        now = datetime.now(timezone.utc)
        # Use the hash of "test-jwt-token" as one of the session IDs
        current_session_id = SessionStore.hash_jwt("test-jwt-token")

        return [
            Session(
                id=current_session_id,
                immich_id=TEST_IMMICH_ID,
                user_id="user_123",
                library_id="lib_456",
                device_type="iOS",
                device_os="iOS 17",
                app_version="1.94.0",
                created_at=now,
                updated_at=now,
                is_pending_sync_reset=False,
            ),
            Session(
                id="other123def456789012345678901234567890123456789012345678901234",
                immich_id=TEST_IMMICH_ID_2,
                user_id="user_123",
                library_id="lib_456",
                device_type="Android",
                device_os="Android 14",
                app_version="1.94.0",
                created_at=now,
                updated_at=now,
                is_pending_sync_reset=False,
            ),
        ]

    @pytest.mark.anyio
    async def test_get_sessions_success(
        self, mock_request, mock_session_store, sample_sessions
    ):
        """Test successful retrieval of sessions."""
        mock_session_store.get_by_user.return_value = sample_sessions
        user_id = UUID("550e8400-e29b-41d4-a716-446655440000")

        result = await get_sessions(
            request=mock_request,
            current_user_id=user_id,
            session_store=mock_session_store,
        )

        assert len(result) == 2
        mock_session_store.get_by_user.assert_called_once_with(str(user_id))

        # Check that exactly one session is marked as current
        current_sessions = [s for s in result if s.current]
        assert len(current_sessions) == 1

        # Verify immich_id is used as the response ID
        response_ids = {s.id for s in result}
        assert str(TEST_IMMICH_ID) in response_ids
        assert str(TEST_IMMICH_ID_2) in response_ids

    @pytest.mark.anyio
    async def test_get_sessions_empty(self, mock_request, mock_session_store):
        """Test retrieval when user has no sessions."""
        mock_session_store.get_by_user.return_value = []
        user_id = UUID("550e8400-e29b-41d4-a716-446655440000")

        result = await get_sessions(
            request=mock_request,
            current_user_id=user_id,
            session_store=mock_session_store,
        )

        assert len(result) == 0


class TestCreateSession:
    """Tests for POST /sessions endpoint."""

    @pytest.mark.anyio
    async def test_create_session_returns_204(self):
        """Test that create_session returns None (204 response)."""
        dto = SessionCreateDto(deviceOS="iOS", deviceType="iOS", duration=3600)

        result = await create_session(dto)

        assert result is None


class TestDeleteAllSessions:
    """Tests for DELETE /sessions endpoint."""

    @pytest.fixture
    def mock_session_store(self):
        """Create a mock SessionStore."""
        store = AsyncMock(spec=SessionStore)
        return store

    @pytest.fixture
    def mock_request(self):
        """Create a mock request with JWT token."""
        request = Mock()
        request.state.jwt_token = "test-jwt-token"
        return request

    @pytest.mark.anyio
    async def test_delete_all_sessions_keeps_current(
        self, mock_request, mock_session_store
    ):
        """Test that delete all sessions keeps the current session."""
        now = datetime.now(timezone.utc)
        current_session_id = SessionStore.hash_jwt("test-jwt-token")

        sessions = [
            Session(
                id=current_session_id,
                immich_id=TEST_IMMICH_ID,
                user_id="user_123",
                library_id="lib_456",
                device_type="iOS",
                device_os="iOS 17",
                app_version="1.0",
                created_at=now,
                updated_at=now,
                is_pending_sync_reset=False,
            ),
            Session(
                id="other123def456789012345678901234567890123456789012345678901234",
                immich_id=TEST_IMMICH_ID_2,
                user_id="user_123",
                library_id="lib_456",
                device_type="Android",
                device_os="Android 14",
                app_version="1.0",
                created_at=now,
                updated_at=now,
                is_pending_sync_reset=False,
            ),
        ]

        mock_session_store.get_by_user.return_value = sessions
        mock_session_store.delete_by_id.return_value = True
        user_id = UUID("550e8400-e29b-41d4-a716-446655440000")

        result = await delete_all_sessions(
            request=mock_request,
            current_user_id=user_id,
            session_store=mock_session_store,
        )

        assert result is None
        # Should only delete the other session, not the current one
        mock_session_store.delete_by_id.assert_called_once_with(sessions[1].id)

    @pytest.mark.anyio
    async def test_delete_all_sessions_no_other_sessions(
        self, mock_request, mock_session_store
    ):
        """Test delete all when only current session exists."""
        now = datetime.now(timezone.utc)
        current_session_id = SessionStore.hash_jwt("test-jwt-token")

        sessions = [
            Session(
                id=current_session_id,
                immich_id=TEST_IMMICH_ID,
                user_id="user_123",
                library_id="lib_456",
                device_type="iOS",
                device_os="iOS 17",
                app_version="1.0",
                created_at=now,
                updated_at=now,
                is_pending_sync_reset=False,
            ),
        ]

        mock_session_store.get_by_user.return_value = sessions
        user_id = UUID("550e8400-e29b-41d4-a716-446655440000")

        result = await delete_all_sessions(
            request=mock_request,
            current_user_id=user_id,
            session_store=mock_session_store,
        )

        assert result is None
        # Should not call delete_by_id at all
        mock_session_store.delete_by_id.assert_not_called()


class TestUpdateSession:
    """Tests for PUT /sessions/{id} endpoint."""

    @pytest.fixture
    def mock_session_store(self):
        """Create a mock SessionStore."""
        store = AsyncMock(spec=SessionStore)
        return store

    @pytest.fixture
    def mock_request(self):
        """Create a mock request with JWT token."""
        request = Mock()
        request.state.jwt_token = "test-jwt-token"
        return request

    @pytest.mark.anyio
    async def test_update_session_success(self, mock_request, mock_session_store):
        """Test successful session update."""
        now = datetime.now(timezone.utc)
        session_id = "abc123def456789012345678901234567890123456789012345678901234"
        session = Session(
            id=session_id,
            immich_id=TEST_IMMICH_ID,
            user_id="user_123",
            library_id="lib_456",
            device_type="iOS",
            device_os="iOS 17",
            app_version="1.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )

        updated_session = Session(
            id=session_id,
            immich_id=TEST_IMMICH_ID,
            user_id="user_123",
            library_id="lib_456",
            device_type="iOS",
            device_os="iOS 17",
            app_version="1.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=True,
        )

        mock_session_store.get_by_immich_id.return_value = session
        mock_session_store.set_pending_sync_reset.return_value = True
        mock_session_store.get_by_id.return_value = updated_session

        user_id = UUID("550e8400-e29b-41d4-a716-446655440000")
        dto = SessionUpdateDto(isPendingSyncReset=True)

        result = await update_session(
            id=TEST_IMMICH_ID,
            session_update=dto,
            request=mock_request,
            current_user_id=user_id,
            session_store=mock_session_store,
        )

        assert result.isPendingSyncReset is True
        assert result.id == str(TEST_IMMICH_ID)
        mock_session_store.get_by_immich_id.assert_called_once_with(
            str(user_id), TEST_IMMICH_ID
        )
        mock_session_store.set_pending_sync_reset.assert_called_once_with(
            session_id, True
        )

    @pytest.mark.anyio
    async def test_update_session_not_found(self, mock_request, mock_session_store):
        """Test update session returns 400 when not found."""
        mock_session_store.get_by_immich_id.return_value = None

        user_id = UUID("550e8400-e29b-41d4-a716-446655440000")
        random_uuid = uuid4()
        dto = SessionUpdateDto(isPendingSyncReset=True)

        with pytest.raises(HTTPException) as exc_info:
            await update_session(
                id=random_uuid,
                session_update=dto,
                request=mock_request,
                current_user_id=user_id,
                session_store=mock_session_store,
            )

        assert exc_info.value.status_code == 400
        assert "Not found" in exc_info.value.detail

    @pytest.mark.anyio
    async def test_update_session_no_changes(self, mock_request, mock_session_store):
        """Test update session with no changes still returns session."""
        now = datetime.now(timezone.utc)
        session_id = "abc123def456789012345678901234567890123456789012345678901234"
        session = Session(
            id=session_id,
            immich_id=TEST_IMMICH_ID,
            user_id="user_123",
            library_id="lib_456",
            device_type="iOS",
            device_os="iOS 17",
            app_version="1.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )

        mock_session_store.get_by_immich_id.return_value = session

        user_id = UUID("550e8400-e29b-41d4-a716-446655440000")
        dto = SessionUpdateDto()  # No changes

        result = await update_session(
            id=TEST_IMMICH_ID,
            session_update=dto,
            request=mock_request,
            current_user_id=user_id,
            session_store=mock_session_store,
        )

        assert result.id == str(TEST_IMMICH_ID)
        # set_pending_sync_reset should not be called
        mock_session_store.set_pending_sync_reset.assert_not_called()


class TestDeleteSession:
    """Tests for DELETE /sessions/{id} endpoint."""

    @pytest.fixture
    def mock_session_store(self):
        """Create a mock SessionStore."""
        store = AsyncMock(spec=SessionStore)
        return store

    @pytest.fixture
    def mock_request(self):
        """Create a mock request with JWT token."""
        request = Mock()
        request.state.jwt_token = "test-jwt-token"
        return request

    @pytest.mark.anyio
    async def test_delete_session_success(self, mock_request, mock_session_store):
        """Test successful session deletion."""
        now = datetime.now(timezone.utc)
        session_id = "abc123def456789012345678901234567890123456789012345678901234"
        session = Session(
            id=session_id,
            immich_id=TEST_IMMICH_ID,
            user_id="user_123",
            library_id="lib_456",
            device_type="iOS",
            device_os="iOS 17",
            app_version="1.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )

        mock_session_store.get_by_immich_id.return_value = session
        mock_session_store.delete_by_id.return_value = True

        user_id = UUID("550e8400-e29b-41d4-a716-446655440000")

        result = await delete_session(
            id=TEST_IMMICH_ID,
            request=mock_request,
            current_user_id=user_id,
            session_store=mock_session_store,
        )

        assert result is None
        mock_session_store.get_by_immich_id.assert_called_once_with(
            str(user_id), TEST_IMMICH_ID
        )
        mock_session_store.delete_by_id.assert_called_once_with(session_id)

    @pytest.mark.anyio
    async def test_delete_session_not_found(self, mock_request, mock_session_store):
        """Test delete session returns 400 when not found."""
        mock_session_store.get_by_immich_id.return_value = None

        user_id = UUID("550e8400-e29b-41d4-a716-446655440000")
        random_uuid = uuid4()

        with pytest.raises(HTTPException) as exc_info:
            await delete_session(
                id=random_uuid,
                request=mock_request,
                current_user_id=user_id,
                session_store=mock_session_store,
            )

        assert exc_info.value.status_code == 400
        assert "Not found or no session.delete access" in exc_info.value.detail


class TestLockSession:
    """Tests for POST /sessions/{id}/lock endpoint."""

    @pytest.mark.anyio
    async def test_lock_session_returns_204(self):
        """Test that lock_session returns None (204 response)."""
        random_uuid = uuid4()

        result = await lock_session(random_uuid)

        assert result is None
