"""Unit tests for WebSocket infrastructure with authentication."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

from pydantic import BaseModel

from services.websockets import (
    _extract_session_token,
    _sid_to_user,
    connect,
    disconnect,
    emit_user_event,
    emit_session_event,
    sio,
    WebSocketEvent,
)
from services.session_store import Session, SessionStoreError


# Test UUIDs for consistent testing
TEST_SESSION_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_USER_ID = "user_123"
TEST_ENCRYPTED_JWT = "gAAAAABh..."


def create_test_session(
    session_id: UUID = TEST_SESSION_ID,
    user_id: str = TEST_USER_ID,
) -> Session:
    """Create a test session."""
    now = datetime.now(timezone.utc)
    return Session(
        id=session_id,
        user_id=user_id,
        library_id="lib_456",
        stored_jwt=TEST_ENCRYPTED_JWT,
        device_type="iOS",
        device_os="iOS 17.4",
        app_version="1.94.0",
        created_at=now,
        updated_at=now,
        is_pending_sync_reset=False,
    )


class TestExtractSessionToken:
    """Test cases for _extract_session_token()."""

    def test_extracts_from_x_immich_user_token_header(self):
        """Test extraction from x-immich-user-token header (mobile)."""
        environ = {"HTTP_X_IMMICH_USER_TOKEN": "my-session-token"}
        result = _extract_session_token(environ)
        assert result == "my-session-token"

    def test_extracts_from_authorization_bearer_header(self):
        """Test extraction from Authorization: Bearer header."""
        environ = {"HTTP_AUTHORIZATION": "Bearer my-bearer-token"}
        result = _extract_session_token(environ)
        assert result == "my-bearer-token"

    def test_extracts_from_authorization_bearer_case_insensitive(self):
        """Test that Bearer prefix matching is case insensitive."""
        environ = {"HTTP_AUTHORIZATION": "bearer my-bearer-token"}
        result = _extract_session_token(environ)
        assert result == "my-bearer-token"

        environ = {"HTTP_AUTHORIZATION": "BEARER my-bearer-token"}
        result = _extract_session_token(environ)
        assert result == "my-bearer-token"

    def test_extracts_from_cookie(self):
        """Test extraction from immich_access_token cookie."""
        environ = {"HTTP_COOKIE": "immich_access_token=my-cookie-token"}
        result = _extract_session_token(environ)
        assert result == "my-cookie-token"

    def test_extracts_from_cookie_with_other_cookies(self):
        """Test extraction when multiple cookies are present."""
        environ = {
            "HTTP_COOKIE": "other_cookie=value; immich_access_token=my-cookie-token; another=xyz"
        }
        result = _extract_session_token(environ)
        assert result == "my-cookie-token"

    def test_priority_header_over_bearer(self):
        """Test that x-immich-user-token takes priority over Bearer."""
        environ = {
            "HTTP_X_IMMICH_USER_TOKEN": "header-token",
            "HTTP_AUTHORIZATION": "Bearer bearer-token",
        }
        result = _extract_session_token(environ)
        assert result == "header-token"

    def test_priority_bearer_over_cookie(self):
        """Test that Bearer takes priority over cookie."""
        environ = {
            "HTTP_AUTHORIZATION": "Bearer bearer-token",
            "HTTP_COOKIE": "immich_access_token=cookie-token",
        }
        result = _extract_session_token(environ)
        assert result == "bearer-token"

    def test_priority_header_over_all(self):
        """Test that x-immich-user-token takes priority over all others."""
        environ = {
            "HTTP_X_IMMICH_USER_TOKEN": "header-token",
            "HTTP_AUTHORIZATION": "Bearer bearer-token",
            "HTTP_COOKIE": "immich_access_token=cookie-token",
        }
        result = _extract_session_token(environ)
        assert result == "header-token"

    def test_returns_none_when_no_token(self):
        """Test that None is returned when no token is found."""
        environ = {}
        result = _extract_session_token(environ)
        assert result is None

    def test_returns_none_for_non_bearer_authorization(self):
        """Test that non-Bearer Authorization headers are ignored."""
        environ = {"HTTP_AUTHORIZATION": "Basic dXNlcjpwYXNz"}
        result = _extract_session_token(environ)
        assert result is None

    def test_returns_none_for_missing_cookie(self):
        """Test that missing immich_access_token cookie returns None."""
        environ = {"HTTP_COOKIE": "other_cookie=value"}
        result = _extract_session_token(environ)
        assert result is None

    def test_handles_malformed_cookie_gracefully(self):
        """Test that malformed cookies don't cause errors."""
        environ = {"HTTP_COOKIE": "malformed"}
        result = _extract_session_token(environ)
        assert result is None

    def test_handles_empty_authorization_header(self):
        """Test that empty Authorization header is handled."""
        environ = {"HTTP_AUTHORIZATION": ""}
        result = _extract_session_token(environ)
        assert result is None

    def test_handles_bearer_only_no_token(self):
        """Test that 'Bearer ' without a token returns None."""
        environ = {"HTTP_AUTHORIZATION": "Bearer "}
        result = _extract_session_token(environ)
        assert result is None


class TestConnectHandler:
    """Integration tests for the connect event handler."""

    @pytest.fixture
    def mock_session_store(self):
        """Create a mock SessionStore."""
        store = AsyncMock()
        session = create_test_session()
        store.get_by_id.return_value = session
        return store

    @pytest.fixture
    def mock_sio(self):
        """Create mock Socket.IO server methods."""
        with patch.object(sio, "enter_room", new_callable=AsyncMock) as mock_enter:
            with patch.object(sio, "emit", new_callable=AsyncMock) as mock_emit:
                yield {"enter_room": mock_enter, "emit": mock_emit}

    @pytest.fixture(autouse=True)
    def clear_sid_tracking(self):
        """Clear the _sid_to_user dict before and after each test."""
        _sid_to_user.clear()
        yield
        _sid_to_user.clear()

    @pytest.mark.anyio
    async def test_rejects_connection_without_token(self, mock_sio):
        """Test that connections without tokens are rejected."""
        environ = {}
        result = await connect("test-sid", environ)
        assert result is False
        mock_sio["enter_room"].assert_not_called()

    @pytest.mark.anyio
    async def test_rejects_connection_with_invalid_session(
        self, mock_session_store, mock_sio
    ):
        """Test that connections with invalid session tokens are rejected."""
        mock_session_store.get_by_id.return_value = None

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "services.websockets.get_session_store",
            mock_get_session_store,
        ):
            environ = {"HTTP_X_IMMICH_USER_TOKEN": "invalid-token"}
            result = await connect("test-sid", environ)
            assert result is False
            mock_sio["enter_room"].assert_not_called()

    @pytest.mark.anyio
    async def test_rejects_connection_on_session_lookup_error(
        self, mock_session_store, mock_sio
    ):
        """Test that connections are rejected when session lookup fails."""
        mock_session_store.get_by_id.side_effect = SessionStoreError(
            "Failed to retrieve session"
        )

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "services.websockets.get_session_store",
            mock_get_session_store,
        ):
            environ = {"HTTP_X_IMMICH_USER_TOKEN": "some-token"}
            result = await connect("test-sid", environ)
            assert result is False
            mock_sio["enter_room"].assert_not_called()

    @pytest.mark.anyio
    async def test_accepts_connection_with_valid_token(
        self, mock_session_store, mock_sio
    ):
        """Test that connections with valid tokens are accepted."""

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "services.websockets.get_session_store",
            mock_get_session_store,
        ):
            environ = {"HTTP_X_IMMICH_USER_TOKEN": str(TEST_SESSION_ID)}
            result = await connect("test-sid", environ)

            # Should not return False (implicit True)
            assert result is None
            # Should join both user room and session room
            assert mock_sio["enter_room"].call_count == 2
            mock_sio["enter_room"].assert_any_call("test-sid", TEST_USER_ID)
            mock_sio["enter_room"].assert_any_call("test-sid", str(TEST_SESSION_ID))
            # Should track session with tuple (user_id, session_id)
            assert _sid_to_user["test-sid"] == (TEST_USER_ID, str(TEST_SESSION_ID))

    @pytest.mark.anyio
    async def test_emits_server_version_on_connect(self, mock_session_store, mock_sio):
        """Test that on_server_version is emitted on successful connect."""

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "services.websockets.get_session_store",
            mock_get_session_store,
        ):
            environ = {"HTTP_X_IMMICH_USER_TOKEN": str(TEST_SESSION_ID)}
            await connect("test-sid", environ)

            # Verify emit was called with on_server_version event
            mock_sio["emit"].assert_called_once()
            call_args = mock_sio["emit"].call_args
            assert call_args[0][0] == "on_server_version"
            assert call_args[1]["room"] == "test-sid"

            # Verify payload structure
            payload = call_args[0][1]
            assert "major" in payload
            assert "minor" in payload
            assert "patch" in payload
            assert "version" in payload

    @pytest.mark.anyio
    async def test_accepts_bearer_token_auth(self, mock_session_store, mock_sio):
        """Test that Bearer token authentication works."""

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "services.websockets.get_session_store",
            mock_get_session_store,
        ):
            environ = {"HTTP_AUTHORIZATION": f"Bearer {TEST_SESSION_ID}"}
            result = await connect("test-sid", environ)
            assert result is None  # Not False = accepted
            assert _sid_to_user["test-sid"] == (TEST_USER_ID, str(TEST_SESSION_ID))

    @pytest.mark.anyio
    async def test_accepts_cookie_auth(self, mock_session_store, mock_sio):
        """Test that cookie authentication works."""

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "services.websockets.get_session_store",
            mock_get_session_store,
        ):
            environ = {"HTTP_COOKIE": f"immich_access_token={TEST_SESSION_ID}"}
            result = await connect("test-sid", environ)
            assert result is None  # Not False = accepted
            assert _sid_to_user["test-sid"] == (TEST_USER_ID, str(TEST_SESSION_ID))

    @pytest.mark.anyio
    async def test_joins_session_room_for_session_delete_events(
        self, mock_session_store, mock_sio
    ):
        """Test that client joins session_id room to receive on_session_delete events.

        This ensures that when a session is deleted from another client (e.g., user
        logs out Browser B from Browser A's settings), Browser B receives the
        on_session_delete event via the session_id room.
        """

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "services.websockets.get_session_store",
            mock_get_session_store,
        ):
            environ = {"HTTP_X_IMMICH_USER_TOKEN": str(TEST_SESSION_ID)}
            await connect("test-sid", environ)

            # Verify client joined the session_id room
            session_room_call = [
                call
                for call in mock_sio["enter_room"].call_args_list
                if call[0][1] == str(TEST_SESSION_ID)
            ]
            assert len(session_room_call) == 1, (
                "Client should join session_id room to receive on_session_delete events"
            )


class TestDisconnectHandler:
    """Tests for the disconnect event handler."""

    @pytest.fixture(autouse=True)
    def clear_sid_tracking(self):
        """Clear the _sid_to_user dict before and after each test."""
        _sid_to_user.clear()
        yield
        _sid_to_user.clear()

    @pytest.mark.anyio
    async def test_removes_socket_from_tracking(self):
        """Test that disconnected sockets are removed from tracking."""
        _sid_to_user["test-sid"] = ("user_123", "session_456")

        await disconnect("test-sid")

        assert "test-sid" not in _sid_to_user

    @pytest.mark.anyio
    async def test_handles_unknown_socket_gracefully(self):
        """Test that disconnecting unknown sockets doesn't raise errors."""
        # Should not raise
        await disconnect("unknown-sid")

        # Dict should remain empty
        assert "unknown-sid" not in _sid_to_user


class TestEmitUserEvent:
    """Unit tests for emit_user_event()."""

    @pytest.fixture
    def mock_sio_emit(self):
        """Mock the sio.emit method."""
        with patch.object(sio, "emit", new_callable=AsyncMock) as mock_emit:
            yield mock_emit

    @pytest.mark.anyio
    async def test_emits_with_string_payload(self, mock_sio_emit):
        """Test emission with a string payload."""
        await emit_user_event(WebSocketEvent.ASSET_DELETE, "user_123", "asset-id-456")

        mock_sio_emit.assert_called_once_with(
            "on_asset_delete",
            "asset-id-456",
            room="user_123",
        )

    @pytest.mark.anyio
    async def test_emits_with_list_payload(self, mock_sio_emit):
        """Test emission with a list payload."""
        asset_ids = ["asset-1", "asset-2", "asset-3"]
        await emit_user_event(WebSocketEvent.ASSET_DELETE, "user_123", asset_ids)

        mock_sio_emit.assert_called_once_with(
            "on_asset_delete",
            asset_ids,
            room="user_123",
        )

    @pytest.mark.anyio
    async def test_serializes_pydantic_model(self, mock_sio_emit):
        """Test that Pydantic models are serialized via model_dump."""

        class TestPayload(BaseModel):
            asset_id: str
            status: str
            count: int

        payload = TestPayload(asset_id="abc-123", status="success", count=42)
        await emit_user_event(WebSocketEvent.UPLOAD_SUCCESS, "user_123", payload)

        mock_sio_emit.assert_called_once_with(
            "on_upload_success",
            {"asset_id": "abc-123", "status": "success", "count": 42},
            room="user_123",
        )

    @pytest.mark.anyio
    async def test_serializes_pydantic_model_with_datetime(self, mock_sio_emit):
        """Test that Pydantic models serialize datetime to ISO format."""

        class TimestampPayload(BaseModel):
            event_time: datetime

        payload = TimestampPayload(
            event_time=datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        )
        await emit_user_event(WebSocketEvent.UPLOAD_SUCCESS, "user_123", payload)

        mock_sio_emit.assert_called_once()
        call_data = mock_sio_emit.call_args[0][1]
        assert call_data["event_time"] == "2024-01-15T10:30:00Z"

    @pytest.mark.anyio
    async def test_emits_to_correct_user_room(self, mock_sio_emit):
        """Test that events are emitted to the correct user room."""
        await emit_user_event(WebSocketEvent.UPLOAD_SUCCESS, "specific-user-id", "data")

        mock_sio_emit.assert_called_once()
        assert mock_sio_emit.call_args[1]["room"] == "specific-user-id"

    @pytest.mark.anyio
    async def test_uses_correct_event_name(self, mock_sio_emit):
        """Test that the correct event name is used from the enum."""
        await emit_user_event(WebSocketEvent.SERVER_VERSION, "user_123", "data")
        assert mock_sio_emit.call_args[0][0] == "on_server_version"

        mock_sio_emit.reset_mock()

        await emit_user_event(WebSocketEvent.ASSET_UPLOAD_READY_V1, "user_123", "data")
        assert mock_sio_emit.call_args[0][0] == "AssetUploadReadyV1"


class TestEmitSessionEvent:
    """Unit tests for emit_session_event()."""

    @pytest.fixture
    def mock_sio_emit(self):
        """Mock the sio.emit method."""
        with patch.object(sio, "emit", new_callable=AsyncMock) as mock_emit:
            yield mock_emit

    @pytest.mark.anyio
    async def test_emits_session_delete_to_session_room(self, mock_sio_emit):
        """Test that SESSION_DELETE is emitted to the session room."""
        await emit_session_event(
            WebSocketEvent.SESSION_DELETE, "session-token-123", "session-token-123"
        )

        mock_sio_emit.assert_called_once_with(
            "on_session_delete",
            "session-token-123",
            room="session-token-123",
        )

    @pytest.mark.anyio
    async def test_emits_with_none_payload(self, mock_sio_emit):
        """Test emission with no payload."""
        await emit_session_event(WebSocketEvent.SESSION_DELETE, "session_123", None)

        mock_sio_emit.assert_called_once_with(
            "on_session_delete",
            None,
            room="session_123",
        )

    @pytest.mark.anyio
    async def test_emits_with_none_payload_default(self, mock_sio_emit):
        """Test emission with payload defaulting to None."""
        await emit_session_event(WebSocketEvent.SESSION_DELETE, "session_123")

        mock_sio_emit.assert_called_once_with(
            "on_session_delete",
            None,
            room="session_123",
        )


class TestWebSocketEventEnum:
    """Tests for WebSocketEvent enum values."""

    def test_event_values(self):
        """Verify all event enum values are correct."""
        assert WebSocketEvent.UPLOAD_SUCCESS.value == "on_upload_success"
        assert WebSocketEvent.ASSET_UPLOAD_READY_V1.value == "AssetUploadReadyV1"
        assert WebSocketEvent.ASSET_DELETE.value == "on_asset_delete"
        assert WebSocketEvent.SESSION_DELETE.value == "on_session_delete"
        assert WebSocketEvent.SERVER_VERSION.value == "on_server_version"
        assert WebSocketEvent.PERSON_THUMBNAIL.value == "on_person_thumbnail"
