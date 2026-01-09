"""Integration tests for OAuth endpoints through auth middleware."""

from datetime import datetime, timezone
from gumnut import omit
import pytest
from unittest.mock import AsyncMock, Mock
from uuid import UUID
from fastapi.testclient import TestClient

from main import app
from routers.utils.cookies import ImmichCookie
from routers.utils.gumnut_client import get_unauthenticated_gumnut_client
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_user_id
from services.session_store import Session, SessionStore, get_session_store

# Test UUIDs
TEST_SESSION_UUID = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_USER_UUID = UUID("660e8400-e29b-41d4-a716-446655440001")
TEST_GUMNUT_USER_ID = uuid_to_gumnut_user_id(TEST_USER_UUID)


@pytest.fixture
def mock_gumnut_client():
    """Create a mock Gumnut client for testing."""
    return Mock()


@pytest.fixture
def mock_session_store():
    """Create a mock SessionStore."""
    store = AsyncMock(spec=SessionStore)
    now = datetime.now(timezone.utc)
    mock_session = Session(
        id=TEST_SESSION_UUID,
        user_id=str(TEST_USER_UUID),
        library_id="",
        stored_jwt="encrypted-jwt",
        device_type="Other",
        device_os="Other",
        app_version="",
        created_at=now,
        updated_at=now,
        is_pending_sync_reset=False,
    )
    store.create.return_value = mock_session
    return store


@pytest.fixture
def client(mock_gumnut_client, mock_session_store):
    """Create a test client with full middleware stack and mocked dependencies."""
    # Override the dependency injection
    app.dependency_overrides[get_unauthenticated_gumnut_client] = (
        lambda: mock_gumnut_client
    )
    app.dependency_overrides[get_session_store] = lambda: mock_session_store
    yield TestClient(app, base_url="https://testserver")
    # Clean up after test
    app.dependency_overrides.clear()


class TestStartOAuthIntegration:
    """Integration tests for POST /api/oauth/authorize endpoint."""

    def test_start_oauth_success(self, client, mock_gumnut_client):
        """Test successful OAuth authorization flow initiation through middleware."""
        oauth_config = {"redirectUri": "http://localhost:3000/auth/callback"}

        # Setup mock response
        mock_auth_url_result = Mock()
        mock_auth_url_result.url = "https://oauth.provider.com/authorize?client_id=test&state=xyz123&redirect_uri=http://localhost:3000/auth/callback"
        mock_gumnut_client.oauth.auth_url.return_value = mock_auth_url_result

        # Execute - POST to the actual endpoint
        response = client.post("/api/oauth/authorize", json=oauth_config)

        # Assert
        assert response.status_code == 201
        result = response.json()
        assert result["url"].startswith("https://oauth.provider.com/authorize")
        assert "state=" in result["url"]
        assert "redirect_uri=" in result["url"]
        mock_gumnut_client.oauth.auth_url.assert_called_once_with(
            redirect_uri="http://localhost:3000/auth/callback",
            code_challenge=None,
            code_challenge_method=None,
            extra_headers={"Authorization": omit},
        )

    def test_start_oauth_with_pkce(self, client, mock_gumnut_client):
        """Test OAuth authorization with PKCE parameters through middleware."""
        oauth_config = {
            "redirectUri": "http://localhost:3000/auth/callback",
            "codeChallenge": "test_challenge_string",
        }

        # Setup mock response
        mock_auth_url_result = Mock()
        mock_auth_url_result.url = "https://oauth.provider.com/authorize?client_id=test&state=xyz123&code_challenge=test_challenge_string"
        mock_gumnut_client.oauth.auth_url.return_value = mock_auth_url_result

        # Execute
        response = client.post("/api/oauth/authorize", json=oauth_config)

        # Assert
        assert response.status_code == 201
        result = response.json()
        assert result["url"].startswith("https://oauth.provider.com/authorize")
        mock_gumnut_client.oauth.auth_url.assert_called_once_with(
            redirect_uri="http://localhost:3000/auth/callback",
            code_challenge="test_challenge_string",
            code_challenge_method="S256",
            extra_headers={"Authorization": omit},
        )

    def test_start_oauth_backend_error(self, client, mock_gumnut_client):
        """Test handling of backend errors during authorization through middleware."""
        oauth_config = {"redirectUri": "http://localhost:3000/auth/callback"}

        # Setup mock to raise an error
        mock_gumnut_client.oauth.auth_url.side_effect = Exception(
            "Backend connection failed"
        )

        # Execute & Assert
        response = client.post("/api/oauth/authorize", json=oauth_config)

        assert response.status_code == 500
        assert (
            "OAuth authentication failed. Please try again."
            == response.json()["message"]
        )


class TestFinishOAuthIntegration:
    """Integration tests for POST /api/oauth/callback endpoint."""

    def test_finish_oauth_success(self, client, mock_gumnut_client, mock_session_store):
        """Test successful OAuth callback flow through middleware."""
        oauth_callback = {
            "url": "http://localhost:3000/auth/callback?code=abc123&state=xyz789"
        }

        # Setup mock response
        mock_exchange_result = Mock()
        mock_exchange_result.access_token = "jwt_token_abc123"
        mock_exchange_result.user = Mock()
        mock_exchange_result.user.id = TEST_GUMNUT_USER_ID
        mock_exchange_result.user.email = "test@example.com"
        mock_exchange_result.user.first_name = "Test"
        mock_exchange_result.user.last_name = "User"
        mock_gumnut_client.oauth.exchange.return_value = mock_exchange_result

        # Execute
        response = client.post("/api/oauth/callback", json=oauth_callback)

        # Assert
        assert response.status_code == 201
        result = response.json()
        expected_session_token = str(TEST_SESSION_UUID)
        assert result["accessToken"] == expected_session_token
        assert result["userId"] == TEST_GUMNUT_USER_ID
        assert result["userEmail"] == "test@example.com"
        assert result["name"] == "Test User"
        assert result["isAdmin"] is False
        assert result["isOnboarded"] is True

        # Verify cookies were set with session token
        assert ImmichCookie.ACCESS_TOKEN.value in response.cookies
        assert ImmichCookie.AUTH_TYPE.value in response.cookies
        assert ImmichCookie.IS_AUTHENTICATED.value in response.cookies

        assert (
            response.cookies[ImmichCookie.ACCESS_TOKEN.value] == expected_session_token
        )
        assert response.cookies[ImmichCookie.AUTH_TYPE.value] == "oauth"
        assert response.cookies[ImmichCookie.IS_AUTHENTICATED.value] == "true"

        # Verify SDK client was called correctly
        mock_gumnut_client.oauth.exchange.assert_called_once_with(
            code="abc123",
            state="xyz789",
            error=None,
            code_verifier=None,
            extra_headers={"Authorization": omit},
        )

        # Verify session was created
        mock_session_store.create.assert_called_once()

    def test_finish_oauth_with_pkce_verifier(
        self, client, mock_gumnut_client, mock_session_store
    ):
        """Test OAuth callback with PKCE code verifier through middleware."""
        oauth_callback = {
            "url": "http://localhost:3000/auth/callback?code=abc123&state=xyz789",
            "codeVerifier": "test_verifier_string",
        }

        # Setup mock response
        mock_exchange_result = Mock()
        mock_exchange_result.access_token = "jwt_token_abc123"
        mock_exchange_result.user = Mock()
        mock_exchange_result.user.id = TEST_GUMNUT_USER_ID
        mock_exchange_result.user.email = "test@example.com"
        mock_exchange_result.user.first_name = "Test"
        mock_exchange_result.user.last_name = "User"
        mock_gumnut_client.oauth.exchange.return_value = mock_exchange_result

        # Execute
        response = client.post("/api/oauth/callback", json=oauth_callback)

        # Assert
        assert response.status_code == 201
        result = response.json()
        assert result["accessToken"] == str(TEST_SESSION_UUID)

        # Verify PKCE verifier was passed to SDK
        mock_gumnut_client.oauth.exchange.assert_called_once_with(
            code="abc123",
            state="xyz789",
            error=None,
            code_verifier="test_verifier_string",
            extra_headers={"Authorization": omit},
        )

    def test_finish_oauth_with_error(self, client, mock_gumnut_client):
        """Test OAuth callback when OAuth provider returned an error through middleware."""
        oauth_callback = {
            "url": "http://localhost:3000/auth/callback?error=access_denied&state=xyz789"
        }

        # Setup mock to raise an error
        mock_gumnut_client.oauth.exchange.side_effect = Exception(
            "OAuth error: {access_denied}"
        )

        # Execute
        response = client.post("/api/oauth/callback", json=oauth_callback)

        assert response.status_code == 500
        assert (
            "OAuth authentication failed. Please try again."
            == response.json()["message"]
        )

        # Verify error was passed to SDK
        mock_gumnut_client.oauth.exchange.assert_called_once_with(
            code=None,
            state="xyz789",
            error="access_denied",
            code_verifier=None,
            extra_headers={"Authorization": omit},
        )

    def test_finish_oauth_invalid_url(self, client):
        """Test handling of invalid callback URL through middleware."""
        oauth_callback = {
            "url": "http://localhost:3000/auth/callback"  # Missing required params
        }

        # Execute & Assert
        response = client.post("/api/oauth/callback", json=oauth_callback)

        assert response.status_code == 400
        assert (
            "OAuth authentication failed. Please try again."
            in response.json()["message"]
        )

    def test_finish_oauth_backend_error(self, client, mock_gumnut_client):
        """Test handling of backend errors during token exchange through middleware."""
        oauth_callback = {
            "url": "http://localhost:3000/auth/callback?code=abc123&state=xyz789"
        }

        # Setup mock to raise an error
        mock_gumnut_client.oauth.exchange.side_effect = Exception(
            "Backend connection failed"
        )

        # Execute & Assert
        response = client.post("/api/oauth/callback", json=oauth_callback)

        assert response.status_code == 500
        assert (
            "OAuth authentication failed. Please try again."
            == response.json()["message"]
        )

    def test_finish_oauth_defaults_optional_fields(
        self, client, mock_gumnut_client, mock_session_store
    ):
        """Test that optional fields in backend response use defaults through middleware."""
        oauth_callback = {
            "url": "http://localhost:3000/auth/callback?code=abc123&state=xyz789"
        }

        # Setup mock response with minimal user data
        mock_exchange_result = Mock()
        mock_exchange_result.access_token = "jwt_token_abc123"
        mock_exchange_result.user = Mock()
        mock_exchange_result.user.id = TEST_GUMNUT_USER_ID
        mock_exchange_result.user.email = "test@example.com"
        mock_exchange_result.user.first_name = "Test"
        mock_exchange_result.user.last_name = "User"
        mock_gumnut_client.oauth.exchange.return_value = mock_exchange_result

        # Execute
        response = client.post("/api/oauth/callback", json=oauth_callback)

        # Assert - optional fields should have default values
        assert response.status_code == 201
        result = response.json()
        assert result["accessToken"] == str(TEST_SESSION_UUID)
        assert result["isAdmin"] is False  # Default
        assert result["isOnboarded"] is True  # Default
        assert result["profileImagePath"] == ""  # Default
        assert result["shouldChangePassword"] is False  # Default
