"""Tests for OAuth endpoints."""

from gumnut import omit
import pytest
from unittest.mock import Mock, patch
from fastapi import HTTPException

from routers.api.oauth import start_oauth, finish_oauth
from routers.immich_models import OAuthConfigDto, OAuthCallbackDto
from routers.utils.cookies import ImmichCookie


@pytest.fixture
def mock_request():
    """Create a mock Request object for testing."""
    mock_req = Mock()
    mock_req.url.scheme = "https"
    return mock_req


class TestStartOAuth:
    """Test the start_oauth (POST /api/oauth/authorize) endpoint."""

    @pytest.mark.anyio
    async def test_start_oauth_success(self):
        """Test successful OAuth authorization flow initiation."""
        # Setup
        oauth_config = OAuthConfigDto(redirectUri="http://localhost:3000/auth/callback")

        # Mock the Gumnut client
        mock_client = Mock()
        mock_auth_url_result = Mock()
        mock_auth_url_result.url = "https://oauth.provider.com/authorize?client_id=test&state=xyz123&redirect_uri=http://localhost:3000/auth/callback"
        mock_client.oauth.auth_url.return_value = mock_auth_url_result

        with patch("routers.api.oauth.get_settings") as mock_get_settings:
            # Mock settings to allow test redirect URI
            mock_settings = Mock()
            mock_settings.oauth_allowed_redirect_uris_list = {
                "http://localhost:3000/auth/callback"
            }
            mock_get_settings.return_value = mock_settings

            # Execute
            result = await start_oauth(oauth_config, mock_client)

            # Assert
            assert result.url.startswith("https://oauth.provider.com/authorize")
            assert "state=" in result.url
            assert "redirect_uri=" in result.url
            mock_client.oauth.auth_url.assert_called_once_with(
                redirect_uri="http://localhost:3000/auth/callback",
                code_challenge=None,
                code_challenge_method=None,
                extra_headers={"Authorization": omit},
            )

    @pytest.mark.anyio
    async def test_start_oauth_with_pkce(self):
        """Test OAuth authorization with PKCE parameters."""
        # Setup
        oauth_config = OAuthConfigDto(
            redirectUri="http://localhost:3000/auth/callback",
            codeChallenge="test_challenge_string",
        )

        # Mock the Gumnut client
        mock_client = Mock()
        mock_auth_url_result = Mock()
        mock_auth_url_result.url = "https://oauth.provider.com/authorize?client_id=test&state=xyz123&code_challenge=test_challenge_string"
        mock_client.oauth.auth_url.return_value = mock_auth_url_result

        with patch("routers.api.oauth.get_settings") as mock_get_settings:
            # Mock settings to allow test redirect URI
            mock_settings = Mock()
            mock_settings.oauth_allowed_redirect_uris_list = {
                "http://localhost:3000/auth/callback"
            }
            mock_get_settings.return_value = mock_settings

            # Execute
            result = await start_oauth(oauth_config, mock_client)

            # Assert
            assert result.url.startswith("https://oauth.provider.com/authorize")
            mock_client.oauth.auth_url.assert_called_once_with(
                redirect_uri="http://localhost:3000/auth/callback",
                code_challenge="test_challenge_string",
                code_challenge_method="S256",
                extra_headers={"Authorization": omit},
            )

    @pytest.mark.anyio
    async def test_start_oauth_invalid_redirect_uri(self):
        """Test that invalid redirect URIs are rejected."""
        # Setup
        oauth_config = OAuthConfigDto(redirectUri="https://evil.com/steal-tokens")
        mock_client = Mock()

        # Execute & Assert
        with pytest.raises(HTTPException) as exc_info:
            await start_oauth(oauth_config, mock_client)

        assert exc_info.value.status_code == 400
        assert "Invalid redirect_uri" in exc_info.value.detail

    @pytest.mark.anyio
    async def test_start_oauth_backend_error(self):
        """Test handling of backend errors during authorization."""
        # Setup
        oauth_config = OAuthConfigDto(redirectUri="http://localhost:3000/auth/callback")

        # Mock the Gumnut client to raise an error
        mock_client = Mock()
        mock_client.oauth.auth_url.side_effect = Exception("Backend connection failed")

        with patch("routers.api.oauth.get_settings") as mock_get_settings:
            # Mock settings to allow test redirect URI
            mock_settings = Mock()
            mock_settings.oauth_allowed_redirect_uris_list = {
                "http://localhost:3000/auth/callback"
            }
            mock_get_settings.return_value = mock_settings

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await start_oauth(oauth_config, mock_client)

            assert exc_info.value.status_code == 500
            assert (
                "OAuth authentication failed. Please try again."
                == exc_info.value.detail
            )


class TestFinishOAuth:
    """Test the finish_oauth (POST /api/oauth/callback) endpoint."""

    @pytest.mark.anyio
    async def test_finish_oauth_success(self, mock_request):
        """Test successful OAuth callback flow."""
        # Setup
        oauth_callback = OAuthCallbackDto(
            url="http://localhost:3000/auth/callback?code=abc123&state=xyz789"
        )
        mock_response = Mock()

        # Mock the Gumnut client
        mock_client = Mock()
        mock_exchange_result = Mock()
        mock_exchange_result.access_token = "jwt_token_abc123"
        mock_exchange_result.user = Mock()
        mock_exchange_result.user.id = "user-uuid-123"
        mock_exchange_result.user.email = "test@example.com"
        mock_exchange_result.user.first_name = "Test"
        mock_exchange_result.user.last_name = "User"
        mock_client.oauth.exchange.return_value = mock_exchange_result

        with patch("routers.api.oauth.parse_callback_url") as mock_parse:
            mock_parse.return_value = {
                "code": "abc123",
                "state": "xyz789",
                "error": None,
            }

            # Execute
            result = await finish_oauth(
                oauth_callback, mock_request, mock_response, mock_client
            )

            # Assert
            assert result.accessToken == "jwt_token_abc123"
            assert result.userId == "user-uuid-123"
            assert result.userEmail == "test@example.com"
            assert result.name == "Test User"
            assert result.isAdmin is False
            assert result.isOnboarded is True

            # Verify cookies were set
            assert mock_response.set_cookie.call_count == 3
            mock_response.set_cookie.assert_any_call(
                key=ImmichCookie.ACCESS_TOKEN.value,
                value="jwt_token_abc123",
                httponly=True,
                secure=True,
                samesite="lax",
            )
            mock_response.set_cookie.assert_any_call(
                key=ImmichCookie.AUTH_TYPE.value,
                value="oauth",
                httponly=True,
                secure=True,
                samesite="lax",
            )
            mock_response.set_cookie.assert_any_call(
                key=ImmichCookie.IS_AUTHENTICATED.value,
                value="true",
                secure=True,
                samesite="lax",
            )

            # Verify SDK client was called correctly
            mock_client.oauth.exchange.assert_called_once_with(
                code="abc123",
                state="xyz789",
                error=None,
                code_verifier=None,
                extra_headers={"Authorization": omit},
            )

    @pytest.mark.anyio
    async def test_finish_oauth_with_pkce_verifier(self, mock_request):
        """Test OAuth callback with PKCE code verifier."""
        # Setup
        oauth_callback = OAuthCallbackDto(
            url="http://localhost:3000/auth/callback?code=abc123&state=xyz789",
            codeVerifier="test_verifier_string",
        )
        mock_response = Mock()

        # Mock the Gumnut client
        mock_client = Mock()
        mock_exchange_result = Mock()
        mock_exchange_result.access_token = "jwt_token_abc123"
        mock_exchange_result.user = Mock()
        mock_exchange_result.user.id = "user-uuid-123"
        mock_exchange_result.user.email = "test@example.com"
        mock_exchange_result.user.first_name = "Test"
        mock_exchange_result.user.last_name = "User"
        mock_client.oauth.exchange.return_value = mock_exchange_result

        with patch("routers.api.oauth.parse_callback_url") as mock_parse:
            mock_parse.return_value = {
                "code": "abc123",
                "state": "xyz789",
                "error": None,
            }

            # Execute
            result = await finish_oauth(
                oauth_callback, mock_request, mock_response, mock_client
            )

            # Assert
            assert result.accessToken == "jwt_token_abc123"

            # Verify PKCE verifier was passed to SDK
            mock_client.oauth.exchange.assert_called_once_with(
                code="abc123",
                state="xyz789",
                error=None,
                code_verifier="test_verifier_string",
                extra_headers={"Authorization": omit},
            )

    @pytest.mark.anyio
    async def test_finish_oauth_with_error(self, mock_request):
        """Test OAuth callback when OAuth provider returned an error."""
        # Setup
        oauth_callback = OAuthCallbackDto(
            url="http://localhost:3000/auth/callback?error=access_denied&state=xyz789"
        )
        mock_response = Mock()

        # Mock the Gumnut client
        mock_client = Mock()
        mock_exchange_result = Mock()
        mock_exchange_result.access_token = "jwt_token_abc123"
        mock_exchange_result.user = Mock()
        mock_exchange_result.user.id = "user-uuid-123"
        mock_exchange_result.user.email = "test@example.com"
        mock_exchange_result.user.first_name = "Test"
        mock_exchange_result.user.last_name = "User"
        mock_client.oauth.exchange.return_value = mock_exchange_result

        with patch("routers.api.oauth.parse_callback_url") as mock_parse:
            mock_parse.return_value = {
                "code": None,
                "state": "xyz789",
                "error": "access_denied",
            }

            # Execute
            result = await finish_oauth(
                oauth_callback, mock_request, mock_response, mock_client
            )

            # Assert - SDK still processes the error
            assert result.accessToken == "jwt_token_abc123"

            # Verify error was passed to SDK
            mock_client.oauth.exchange.assert_called_once_with(
                code=None,
                state="xyz789",
                error="access_denied",
                code_verifier=None,
                extra_headers={"Authorization": omit},
            )

    @pytest.mark.anyio
    async def test_finish_oauth_invalid_url(self, mock_request):
        """Test handling of invalid callback URL."""
        # Setup
        oauth_callback = OAuthCallbackDto(
            url="http://localhost:3000/auth/callback"  # Missing required params
        )
        mock_response = Mock()
        mock_client = Mock()

        with patch("routers.api.oauth.parse_callback_url") as mock_parse:
            mock_parse.side_effect = ValueError("Missing required 'state' parameter")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await finish_oauth(
                    oauth_callback, mock_request, mock_response, mock_client
                )

            assert exc_info.value.status_code == 400
            assert "Missing required 'state' parameter" in str(exc_info.value.detail)

    @pytest.mark.anyio
    async def test_finish_oauth_backend_error(self, mock_request):
        """Test handling of backend errors during token exchange."""
        # Setup
        oauth_callback = OAuthCallbackDto(
            url="http://localhost:3000/auth/callback?code=abc123&state=xyz789"
        )
        mock_response = Mock()

        # Mock the Gumnut client to raise an error
        mock_client = Mock()
        mock_client.oauth.exchange.side_effect = Exception("Backend connection failed")

        with patch("routers.api.oauth.parse_callback_url") as mock_parse:
            mock_parse.return_value = {
                "code": "abc123",
                "state": "xyz789",
                "error": None,
            }

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await finish_oauth(
                    oauth_callback, mock_request, mock_response, mock_client
                )

            assert exc_info.value.status_code == 500
            assert (
                "OAuth authentication failed. Please try again."
                == exc_info.value.detail
            )

    @pytest.mark.anyio
    async def test_finish_oauth_defaults_optional_fields(self, mock_request):
        """Test that optional fields in backend response use defaults."""
        # Setup
        oauth_callback = OAuthCallbackDto(
            url="http://localhost:3000/auth/callback?code=abc123&state=xyz789"
        )
        mock_response = Mock()

        # Mock the Gumnut client with minimal user data
        mock_client = Mock()
        mock_exchange_result = Mock()
        mock_exchange_result.access_token = "jwt_token_abc123"
        mock_exchange_result.user = Mock()
        mock_exchange_result.user.id = "user-uuid-123"
        mock_exchange_result.user.email = "test@example.com"
        mock_exchange_result.user.first_name = "Test"
        mock_exchange_result.user.last_name = "User"
        mock_client.oauth.exchange.return_value = mock_exchange_result

        with patch("routers.api.oauth.parse_callback_url") as mock_parse:
            mock_parse.return_value = {
                "code": "abc123",
                "state": "xyz789",
                "error": None,
            }

            # Execute
            result = await finish_oauth(
                oauth_callback, mock_request, mock_response, mock_client
            )

            # Assert - optional fields should have default values
            assert result.accessToken == "jwt_token_abc123"
            assert result.isAdmin is False  # Default
            assert result.isOnboarded is True  # Default
            assert result.profileImagePath == ""  # Default
            assert result.shouldChangePassword is False  # Default
