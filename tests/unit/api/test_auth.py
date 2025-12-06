"""Unit tests for Auth API functions."""

from unittest.mock import AsyncMock, Mock

import pytest

from routers.api.auth import post_logout
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
        request.state = Mock(spec=[])  # No jwt_token attribute by default
        return request

    @pytest.fixture
    def mock_response(self):
        """Create a mock response."""
        return Mock()

    @pytest.mark.anyio
    async def test_deletes_session_from_request_state(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that session is deleted when JWT is in request.state."""
        mock_request.state.jwt_token = "test-jwt-token"
        mock_request.cookies = {}

        result = await post_logout(
            request=mock_request,
            response=mock_response,
            client=None,
            session_store=mock_session_store,
        )

        # Verify session was deleted
        mock_session_store.delete.assert_called_once_with("test-jwt-token")
        assert result.successful is True

    @pytest.mark.anyio
    async def test_deletes_session_from_cookie_when_not_in_state(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that session is deleted when JWT is in cookie but not in state."""
        mock_request.state = Mock(spec=[])  # No jwt_token attribute
        mock_request.cookies = {
            ImmichCookie.ACCESS_TOKEN.value: "cookie-jwt-token",
        }

        result = await post_logout(
            request=mock_request,
            response=mock_response,
            client=None,
            session_store=mock_session_store,
        )

        # Verify session was deleted using cookie value
        mock_session_store.delete.assert_called_once_with("cookie-jwt-token")
        assert result.successful is True

    @pytest.mark.anyio
    async def test_logout_succeeds_when_no_jwt_present(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout succeeds even when no JWT is present."""
        mock_request.state = Mock(spec=[])  # No jwt_token attribute
        mock_request.cookies = {}

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
        mock_request.state.jwt_token = "test-jwt-token"
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
        mock_request.state.jwt_token = "test-jwt-token"
        mock_request.cookies = {}

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

        result = await post_logout(
            request=mock_request,
            response=mock_response,
            client=None,
            session_store=mock_session_store,
        )

        assert result.redirectUri == "/auth/login?autoLaunch=0"
        assert result.successful is True
