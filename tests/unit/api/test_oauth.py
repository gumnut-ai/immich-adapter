"""Unit tests for OAuth API functions."""

from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from fastapi import HTTPException, Request
from starlette.datastructures import URL
import pytest

from routers.api.oauth import finish_oauth, rewrite_redirect_uri
from routers.immich_models import OAuthCallbackDto
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_user_id
from services.session_store import SessionStore

TEST_USER_UUID = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_GUMNUT_USER_ID = uuid_to_gumnut_user_id(TEST_USER_UUID)


class TestRewriteRedirectUri:
    """Test the rewrite_redirect_uri function."""

    def test_non_mobile_redirect_uri_unchanged(self):
        """Test that non-mobile redirect URIs are returned unchanged."""
        # Create a mock request
        request = Mock(spec=Request)
        request.headers.get.return_value = None

        # Test with regular HTTP redirect URI
        result = rewrite_redirect_uri("http://localhost:3000/auth/callback", request)
        assert result == "http://localhost:3000/auth/callback"

        # Test with HTTPS redirect URI
        result = rewrite_redirect_uri("https://example.com/callback", request)
        assert result == "https://example.com/callback"

    def test_mobile_redirect_with_proxy_headers(self):
        """Test mobile redirect URI rewriting with X-Forwarded-* headers (behind proxy)."""
        # Create a mock request with proxy headers
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-proto": "https",
        }.get(key)

        # Mock url_for to return a real URL object (simulating internal http URL)
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should use proxy headers to build the URL - scheme and host from headers
        assert result == "https://localhost:3001/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_without_proxy_headers(self):
        """Test mobile redirect URI rewriting without proxy headers (direct connection)."""
        # Create a mock request without proxy headers
        request = Mock(spec=Request)
        request.headers.get.return_value = None

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should use request.url_for() directly without modification
        assert result == "http://localhost:3001/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_with_multiple_proxy_headers(self):
        """Test handling of comma-separated proxy headers (multiple proxies)."""
        # Create a mock request with comma-separated proxy headers
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-proto": "https, http",  # Multiple values
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://adapter.gumnut.com/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should use first value from comma-separated list
        assert result == "https://adapter.gumnut.com/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_with_whitespace_in_headers(self):
        """Test handling of whitespace in proxy headers."""
        # Create a mock request with whitespace in headers
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-proto": " https ",
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should strip whitespace from headers
        assert result == "https://localhost:3001/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_with_invalid_scheme_falls_back(self):
        """Test that invalid scheme in X-Forwarded-Proto falls back to url_for."""
        # Create a mock request with invalid scheme
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-proto": "ftp",  # Invalid scheme
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should fall back to url_for since scheme is invalid
        assert result == "http://localhost:3001/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_case_sensitivity(self):
        """Test that scheme comparison is case-insensitive."""
        # Create a mock request with uppercase scheme
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-proto": "HTTPS",  # Uppercase
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://adapter.gumnut.com/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should normalize to lowercase scheme
        assert result == "https://adapter.gumnut.com/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")


class TestFinishOAuth:
    """Tests for OAuth callback session creation."""

    @pytest.fixture
    def mock_session_store(self):
        """Create a mock SessionStore."""
        store = AsyncMock(spec=SessionStore)
        return store

    @pytest.fixture
    def mock_gumnut_client(self):
        """Create a mock Gumnut client."""
        client = Mock()
        client.oauth = Mock()
        return client

    @pytest.fixture
    def mock_request(self):
        """Create a mock request."""
        request = Mock()
        request.headers = {}
        request.url = Mock()
        request.url.scheme = "https"
        return request

    @pytest.fixture
    def mock_response(self):
        """Create a mock response."""
        return Mock()

    @pytest.mark.anyio
    async def test_creates_session_on_successful_oauth(
        self, mock_request, mock_response, mock_gumnut_client, mock_session_store
    ):
        """Test that session is created on successful OAuth callback."""
        # Setup mock OAuth exchange result
        mock_user = Mock()
        mock_user.id = TEST_GUMNUT_USER_ID
        mock_user.email = "test@example.com"
        mock_user.first_name = "Test"
        mock_user.last_name = "User"

        mock_exchange_result = Mock()
        mock_exchange_result.access_token = "test-jwt-token"
        mock_exchange_result.user = mock_user

        mock_gumnut_client.oauth.exchange.return_value = mock_exchange_result
        # Use realistic Chrome on Mac UA string
        mock_request.headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        callback_dto = OAuthCallbackDto(
            url="http://localhost/callback?code=auth_code&state=state_token"
        )

        with patch("routers.api.oauth.parse_callback_url") as mock_parse:
            mock_parse.return_value = {
                "code": "auth_code",
                "state": "state_token",
                "error": None,
            }

            await finish_oauth(
                oauth_callback=callback_dto,
                request=mock_request,
                response=mock_response,
                client=mock_gumnut_client,
                session_store=mock_session_store,
            )

        # Verify session was created with correct device info
        # user-agents library parses this UA as: browser.family=Chrome, os.family=Mac OS X
        # Our code normalizes "Mac OS X" to "macOS" for Immich frontend compatibility
        mock_session_store.create.assert_called_once()
        call_kwargs = mock_session_store.create.call_args.kwargs
        assert call_kwargs["jwt_token"] == "test-jwt-token"
        assert call_kwargs["user_id"] == str(TEST_USER_UUID)
        assert call_kwargs["library_id"] == ""
        assert call_kwargs["device_type"] == "Chrome"
        assert call_kwargs["device_os"] == "macOS"
        assert call_kwargs["app_version"] == ""

    @pytest.mark.anyio
    async def test_login_fails_if_session_creation_fails(
        self, mock_request, mock_response, mock_gumnut_client, mock_session_store
    ):
        """Test that login fails if session creation fails.

        Session creation is required because we return the session token to clients.
        If we can't create a session, we can't authenticate the user.
        """
        mock_user = Mock()
        mock_user.id = TEST_GUMNUT_USER_ID
        mock_user.email = "test@example.com"
        mock_user.first_name = "Test"
        mock_user.last_name = "User"

        mock_exchange_result = Mock()
        mock_exchange_result.access_token = "test-jwt-token"
        mock_exchange_result.user = mock_user

        mock_gumnut_client.oauth.exchange.return_value = mock_exchange_result
        mock_request.headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        # Session creation will fail
        mock_session_store.create.side_effect = Exception("Redis connection failed")

        callback_dto = OAuthCallbackDto(
            url="http://localhost/callback?code=auth_code&state=state_token"
        )

        with patch("routers.api.oauth.parse_callback_url") as mock_parse:
            mock_parse.return_value = {
                "code": "auth_code",
                "state": "state_token",
                "error": None,
            }

            # Should raise an HTTPException since session creation is required
            with pytest.raises(HTTPException) as exc_info:
                await finish_oauth(
                    oauth_callback=callback_dto,
                    request=mock_request,
                    response=mock_response,
                    client=mock_gumnut_client,
                    session_store=mock_session_store,
                )

            assert exc_info.value.status_code == 500
            assert "OAuth authentication failed" in exc_info.value.detail
