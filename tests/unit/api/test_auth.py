"""Unit tests for Auth API functions."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from socketio.exceptions import SocketIOError

from routers.api.auth import post_logout
from services.websockets import WebSocketEvent
from routers.utils.cookies import ImmichCookie
from services.session_store import SessionStore


class TestPostLogout:
    """Tests for logout session deletion."""

    @pytest.fixture
    def mock_session_store(self):
        """Create a mock SessionStore."""
        store = AsyncMock(spec=SessionStore)
        return store

    @pytest.fixture
    def mock_request(self):
        """Create a mock request."""
        request = Mock()
        request.cookies = {}
        request.state = Mock(spec=[])  # No session_token attribute by default
        return request

    @pytest.fixture
    def mock_response(self):
        """Create a mock response."""
        return Mock()

    @pytest.mark.anyio
    async def test_deletes_session_from_request_state(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that session is deleted when session token is in request.state."""
        mock_request.state.session_token = "test-session-token"
        mock_request.cookies = {}

        with patch("routers.api.auth.emit_event", new_callable=AsyncMock):
            result = await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

        # Verify session was deleted
        mock_session_store.delete.assert_called_once_with("test-session-token")
        assert result.successful is True

    @pytest.mark.anyio
    async def test_deletes_session_from_cookie_when_not_in_state(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that session is deleted when session token is in cookie but not in state."""
        mock_request.state = Mock(spec=[])  # No session_token attribute
        mock_request.cookies = {
            ImmichCookie.ACCESS_TOKEN.value: "cookie-session-token",
        }

        with patch("routers.api.auth.emit_event", new_callable=AsyncMock):
            result = await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

        # Verify session was deleted using cookie value
        mock_session_store.delete.assert_called_once_with("cookie-session-token")
        assert result.successful is True

    @pytest.mark.anyio
    async def test_logout_succeeds_when_no_jwt_present(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout succeeds even when no session token is present."""
        mock_request.state = Mock(spec=[])  # No session_token attribute
        mock_request.cookies = {}

        with patch("routers.api.auth.emit_event", new_callable=AsyncMock):
            result = await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

        # Session delete should not be called
        mock_session_store.delete.assert_not_called()
        # But logout should still succeed
        assert result.successful is True

    @pytest.mark.anyio
    async def test_logout_succeeds_when_session_deletion_fails(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout succeeds even if session deletion fails."""
        mock_request.state.session_token = "test-session-token"
        mock_request.cookies = {}

        # Session deletion will fail
        mock_session_store.delete.side_effect = Exception("Redis connection failed")

        result = await post_logout(
            request=mock_request,
            response=mock_response,
            client=None,
            session_store=mock_session_store,
        )

        # Logout should still succeed
        assert result.successful is True
        # Cookies should still be deleted
        mock_response.delete_cookie.assert_any_call(ImmichCookie.ACCESS_TOKEN.value)
        mock_response.delete_cookie.assert_any_call(ImmichCookie.AUTH_TYPE.value)
        mock_response.delete_cookie.assert_any_call(ImmichCookie.IS_AUTHENTICATED.value)

    @pytest.mark.anyio
    async def test_logout_deletes_cookies(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout always deletes auth cookies."""
        mock_request.state.session_token = "test-session-token"
        mock_request.cookies = {}

        with patch("routers.api.auth.emit_event", new_callable=AsyncMock):
            await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

        # All auth cookies should be deleted
        mock_response.delete_cookie.assert_any_call(ImmichCookie.ACCESS_TOKEN.value)
        mock_response.delete_cookie.assert_any_call(ImmichCookie.AUTH_TYPE.value)
        mock_response.delete_cookie.assert_any_call(ImmichCookie.IS_AUTHENTICATED.value)

    @pytest.mark.anyio
    async def test_logout_returns_correct_redirect_uri(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout returns the correct redirect URI for non-OAuth logout."""
        mock_request.state = Mock(spec=[])
        mock_request.cookies = {}

        with patch("routers.api.auth.emit_event", new_callable=AsyncMock):
            result = await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

        assert result.redirectUri == "/auth/login?autoLaunch=0"
        assert result.successful is True

    @pytest.mark.anyio
    async def test_logout_emits_websocket_event(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout emits on_session_delete WebSocket event."""
        mock_request.state.session_token = "test-session-token"
        mock_request.cookies = {}

        with patch("routers.api.auth.emit_event", new_callable=AsyncMock) as mock_emit:
            await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

            mock_emit.assert_called_once()
            call = mock_emit.call_args
            assert call[0][0] == WebSocketEvent.SESSION_DELETE
            assert call[0][1] == "test-session-token"
            assert call[0][2] == "test-session-token"

    @pytest.mark.anyio
    async def test_logout_websocket_error_does_not_fail_logout(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that WebSocket emission errors don't fail logout."""
        mock_request.state.session_token = "test-session-token"
        mock_request.cookies = {}

        with patch(
            "routers.api.auth.emit_event",
            new_callable=AsyncMock,
            side_effect=SocketIOError("WebSocket error"),
        ):
            result = await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

            # Logout should still succeed despite WebSocket error
            assert result.successful is True
            mock_session_store.delete.assert_called_once()
