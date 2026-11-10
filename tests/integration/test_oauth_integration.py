"""Integration tests for OAuth endpoints through auth middleware."""

import base64
import hashlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import fakeredis
import pytest
from fastapi.testclient import TestClient
from gumnut import omit

from main import app
from routers.utils.cookies import ImmichCookie
from routers.utils.gumnut_client import get_unauthenticated_gumnut_client
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_id
from services.oauth_sign_in_store import OAuthSignInStore, get_oauth_sign_in_store
from services.session_store import Session, SessionStore, get_session_store

# Test UUIDs
TEST_SESSION_UUID = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_USER_UUID = UUID("660e8400-e29b-41d4-a716-446655440001")
TEST_GUMNUT_USER_ID = uuid_to_gumnut_id(TEST_USER_UUID, "intuser")


def s256(verifier: str) -> str:
    """RFC 7636 S256 code challenge for ``verifier``."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generated_verifier(gumnut_client) -> str:
    """The verifier the last exchange presented, checked against the challenge
    the last authorize sent."""
    authorize = gumnut_client.oauth.auth_url.call_args.kwargs
    verifier = gumnut_client.oauth.exchange.call_args.kwargs["code_verifier"]
    assert authorize["code_challenge_method"] == "S256"
    assert 43 <= len(verifier) <= 128
    challenge = authorize["code_challenge"]
    assert s256(verifier) == challenge
    return verifier


@pytest.fixture
def mock_gumnut_client():
    """Create a mock Gumnut client for testing."""
    return Mock()


@pytest.fixture
def mock_session_store():
    """Create a mock SessionStore."""
    store = AsyncMock(spec=SessionStore)
    now = datetime.now(UTC)
    mock_session = Session(
        token=TEST_SESSION_UUID,
        user_id=str(TEST_USER_UUID),
        library_id="",
        stored_jwt="encrypted-jwt",
        device_type="Other",
        device_os="Other",
        app_version="",
        created_at=now,
        updated_at=now,
    )
    store.create.return_value = mock_session
    return store


@pytest.fixture
def client(mock_gumnut_client, mock_session_store, sign_in_store):
    """Create a test client with full middleware stack and mocked dependencies."""
    # Override the dependency injection
    app.dependency_overrides[get_unauthenticated_gumnut_client] = lambda: (
        mock_gumnut_client
    )
    app.dependency_overrides[get_session_store] = lambda: mock_session_store
    app.dependency_overrides[get_oauth_sign_in_store] = lambda: sign_in_store
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
        mock_gumnut_client.oauth.auth_url = AsyncMock(return_value=mock_auth_url_result)

        # Execute - POST to the actual endpoint
        response = client.post("/api/oauth/authorize", json=oauth_config)

        # Assert
        assert response.status_code == 201
        result = response.json()
        assert result["url"].startswith("https://oauth.provider.com/authorize")
        assert "state=" in result["url"]
        assert "redirect_uri=" in result["url"]
        # The adapter generated a PKCE challenge for a client that sent none.
        mock_gumnut_client.oauth.auth_url.assert_called_once()
        kwargs = mock_gumnut_client.oauth.auth_url.call_args.kwargs
        assert kwargs["redirect_uri"] == "http://localhost:3000/auth/callback"
        assert kwargs["code_challenge_method"] == "S256"
        assert len(kwargs["code_challenge"]) == 43
        assert kwargs["extra_headers"] == {"Authorization": omit}

    def test_start_oauth_with_pkce(self, client, mock_gumnut_client):
        """Test OAuth authorization with PKCE parameters through middleware."""
        oauth_config = {
            "redirectUri": "http://localhost:3000/auth/callback",
            "codeChallenge": "test_challenge_string",
        }

        # Setup mock response
        mock_auth_url_result = Mock()
        mock_auth_url_result.url = "https://oauth.provider.com/authorize?client_id=test&state=xyz123&code_challenge=test_challenge_string"
        mock_gumnut_client.oauth.auth_url = AsyncMock(return_value=mock_auth_url_result)

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
        mock_gumnut_client.oauth.auth_url = AsyncMock(
            side_effect=Exception("Backend connection failed")
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

    @pytest.fixture(autouse=True)
    def started_sign_in(self, client, mock_gumnut_client):
        """Start sign-in from this client for the state the callbacks carry."""
        auth_url_result = Mock()
        auth_url_result.url = "https://oauth.provider.com/authorize?state=xyz789"
        mock_gumnut_client.oauth.auth_url = AsyncMock(return_value=auth_url_result)
        response = client.post(
            "/api/oauth/authorize",
            json={"redirectUri": "http://localhost:3000/auth/callback"},
        )
        assert response.status_code == 201

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
        mock_gumnut_client.oauth.exchange = AsyncMock(return_value=mock_exchange_result)

        # Execute
        response = client.post("/api/oauth/callback", json=oauth_callback)

        # Assert
        assert response.status_code == 201
        result = response.json()
        expected_session_token = str(TEST_SESSION_UUID)
        assert result["accessToken"] == expected_session_token
        assert result["userId"] == str(TEST_USER_UUID)
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

        # Verify SDK client was called correctly, presenting the verifier the
        # adapter generated at authorize
        mock_gumnut_client.oauth.exchange.assert_called_once_with(
            code="abc123",
            state="xyz789",
            error=None,
            code_verifier=generated_verifier(mock_gumnut_client),
            extra_headers={"Authorization": omit},
        )

        # Verify session was created
        mock_session_store.create.assert_called_once()

    def test_finish_oauth_with_pkce_verifier(
        self, client, mock_gumnut_client, mock_session_store
    ):
        """Test OAuth callback with PKCE code verifier through middleware."""
        # This client starts its sign-in with its own PKCE challenge.
        client.post(
            "/api/oauth/authorize",
            json={
                "redirectUri": "http://localhost:3000/auth/callback",
                "codeChallenge": "test_challenge_string",
            },
        )
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
        mock_gumnut_client.oauth.exchange = AsyncMock(return_value=mock_exchange_result)

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
        mock_gumnut_client.oauth.exchange = AsyncMock(
            side_effect=Exception("OAuth error: {access_denied}")
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
            code_verifier=generated_verifier(mock_gumnut_client),
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
        mock_gumnut_client.oauth.exchange = AsyncMock(
            side_effect=Exception("Backend connection failed")
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
        mock_gumnut_client.oauth.exchange = AsyncMock(return_value=mock_exchange_result)

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


WEB_CALLBACK = "https://testserver/auth/login"
MOBILE_REDIRECT = "app.immich:///oauth-callback"


class TestOAuthSignInBinding:
    """A callback completes sign-in only for the client that started it.

    Each TestClient has its own cookie jar, so two clients stand in for two
    browsers (or a native app that does not send cookies).
    """

    @pytest.fixture
    def redis_server(self):
        return fakeredis.FakeServer()

    @pytest.fixture
    def sign_in_store(self, redis_server):
        """A real store whose Redis the test can also reach synchronously."""
        return OAuthSignInStore(
            fakeredis.FakeAsyncRedis(server=redis_server, decode_responses=True)
        )

    @pytest.fixture
    def backend(self, mock_gumnut_client):
        """Fake the Gumnut API: each authorize mints the next state."""
        states = iter(["server-state-1", "server-state-2"])

        async def auth_url(**kwargs):
            result = Mock()
            result.url = (
                "https://idp.example.com/authorize?client_id=test"
                f"&state={next(states)}&redirect_uri={kwargs['redirect_uri']}"
            )
            return result

        exchange_result = Mock()
        exchange_result.access_token = "jwt_token_abc123"
        exchange_result.user = Mock()
        exchange_result.user.id = TEST_GUMNUT_USER_ID
        exchange_result.user.email = "test@example.com"
        exchange_result.user.first_name = "Test"
        exchange_result.user.last_name = "User"

        mock_gumnut_client.oauth.auth_url = AsyncMock(side_effect=auth_url)
        mock_gumnut_client.oauth.exchange = AsyncMock(return_value=exchange_result)
        return mock_gumnut_client

    @pytest.fixture
    def other_client(self, client):
        """A second client with its own, empty cookie jar."""
        return TestClient(app, base_url="https://testserver")

    @staticmethod
    def callback_url(state: str, base: str = WEB_CALLBACK) -> str:
        return f"{base}?code=abc123&state={state}"

    def assert_rejected(self, response, backend, mock_session_store):
        assert response.status_code == 400
        assert "start sign-in again" in response.json()["message"]
        backend.oauth.exchange.assert_not_called()
        mock_session_store.create.assert_not_called()
        assert ImmichCookie.ACCESS_TOKEN.value not in response.cookies

    def test_authorize_sets_short_lived_http_only_sign_in_cookie(self, client, backend):
        response = client.post(
            "/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK}
        )

        assert response.status_code == 201
        [cookie] = [
            header
            for header in response.headers.get_list("set-cookie")
            if header.startswith(f"{ImmichCookie.OAUTH_SIGN_IN.value}=")
        ]
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=lax" in cookie
        assert "Max-Age=600" in cookie
        # A fresh secret, not the state that the callback URL will carry.
        secret = response.cookies[ImmichCookie.OAUTH_SIGN_IN.value]
        assert len(secret) >= 40
        assert "server-state-1" not in secret

    def test_authorize_fails_when_backend_url_has_no_state(
        self, client, mock_gumnut_client
    ):
        result = Mock()
        result.url = "https://idp.example.com/authorize?client_id=test"
        mock_gumnut_client.oauth.auth_url = AsyncMock(return_value=result)

        response = client.post(
            "/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK}
        )

        assert response.status_code == 500
        assert ImmichCookie.OAUTH_SIGN_IN.value not in response.cookies

    def test_browser_that_started_sign_in_completes_it(
        self, client, backend, mock_session_store
    ):
        client.post("/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK})

        response = client.post(
            "/api/oauth/callback", json={"url": self.callback_url("server-state-1")}
        )

        assert response.status_code == 201
        assert response.json()["userId"] == str(TEST_USER_UUID)
        backend.oauth.exchange.assert_called_once_with(
            code="abc123",
            state="server-state-1",
            error=None,
            code_verifier=generated_verifier(backend),
            extra_headers={"Authorization": omit},
        )
        mock_session_store.create.assert_called_once()
        # Sign-in completed, so the sign-in cookie is cleared.
        assert ImmichCookie.OAUTH_SIGN_IN.value not in client.cookies

    def test_other_browser_cannot_complete_sign_in(
        self, client, other_client, backend, mock_session_store
    ):
        client.post("/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK})

        response = other_client.post(
            "/api/oauth/callback", json={"url": self.callback_url("server-state-1")}
        )

        self.assert_rejected(response, backend, mock_session_store)

        # The rejected callback did not use up the sign-in: the browser that
        # started it can still complete it.
        response = client.post(
            "/api/oauth/callback", json={"url": self.callback_url("server-state-1")}
        )
        assert response.status_code == 201

    def test_browser_with_cookie_from_another_sign_in_is_rejected(
        self, client, other_client, backend, mock_session_store
    ):
        client.post("/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK})
        other_client.post("/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK})

        response = other_client.post(
            "/api/oauth/callback", json={"url": self.callback_url("server-state-1")}
        )

        self.assert_rejected(response, backend, mock_session_store)

    def test_cookie_holding_the_url_state_does_not_complete_sign_in(
        self, client, other_client, backend, mock_session_store
    ):
        # The callback URL's state is no proof: only the secret issued at
        # authorize is.
        client.post("/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK})
        other_client.cookies.set(ImmichCookie.OAUTH_SIGN_IN.value, "server-state-1")

        response = other_client.post(
            "/api/oauth/callback", json={"url": self.callback_url("server-state-1")}
        )

        self.assert_rejected(response, backend, mock_session_store)

    def test_callback_without_state_is_rejected(
        self, client, backend, mock_session_store
    ):
        client.post(
            "/api/oauth/authorize",
            json={"redirectUri": WEB_CALLBACK, "state": "client-state"},
        )

        response = client.post(
            "/api/oauth/callback",
            json={"url": f"{WEB_CALLBACK}?code=abc123", "state": "client-state"},
        )

        assert response.status_code == 400
        backend.oauth.exchange.assert_not_called()
        mock_session_store.create.assert_not_called()

    def test_native_client_that_started_sign_in_completes_it(
        self, client, other_client, backend, mock_session_store
    ):
        # Model a native client that sends no cookies: it calls back from a fresh jar
        # and proves itself with the state it sent at authorize.
        client.post(
            "/api/oauth/authorize",
            json={
                "redirectUri": MOBILE_REDIRECT,
                "state": "client-state",
                "codeChallenge": "challenge",
            },
        )

        response = other_client.post(
            "/api/oauth/callback",
            json={
                "url": self.callback_url("server-state-1", base=MOBILE_REDIRECT),
                "state": "client-state",
                "codeVerifier": "verifier",
            },
        )

        assert response.status_code == 201
        # The client's own PKCE pair passes through untouched.
        authorize = backend.oauth.auth_url.call_args.kwargs
        assert authorize["code_challenge"] == "challenge"
        assert authorize["code_challenge_method"] == "S256"
        backend.oauth.exchange.assert_called_once_with(
            code="abc123",
            state="server-state-1",
            error=None,
            code_verifier="verifier",
            extra_headers={"Authorization": omit},
        )
        mock_session_store.create.assert_called_once()

    def test_generated_verifier_never_reaches_the_browser(self, client, backend):
        authorize = client.post(
            "/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK}
        )
        client.post(
            "/api/oauth/callback", json={"url": self.callback_url("server-state-1")}
        )

        verifier = generated_verifier(backend)
        assert verifier not in authorize.text
        assert verifier not in str(authorize.headers.get_list("set-cookie"))

    def test_browser_cannot_substitute_its_own_verifier(self, client, backend):
        client.post("/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK})

        response = client.post(
            "/api/oauth/callback",
            json={
                "url": self.callback_url("server-state-1"),
                "codeVerifier": "verifier-chosen-by-the-caller",
            },
        )

        assert response.status_code == 201
        assert generated_verifier(backend) != "verifier-chosen-by-the-caller"

    def test_each_sign_in_gets_its_own_pkce_pair(self, client, other_client, backend):
        client.post("/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK})
        other_client.post("/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK})

        first, second = (
            call.kwargs["code_challenge"]
            for call in backend.oauth.auth_url.call_args_list
        )
        assert first != second

    def test_native_client_without_its_own_challenge_presents_generated_verifier(
        self, client, other_client, backend, mock_session_store
    ):
        # Proves itself with its state, from a fresh cookie jar.
        client.post(
            "/api/oauth/authorize",
            json={"redirectUri": MOBILE_REDIRECT, "state": "client-state"},
        )

        response = other_client.post(
            "/api/oauth/callback",
            json={
                "url": self.callback_url("server-state-1", base=MOBILE_REDIRECT),
                "state": "client-state",
            },
        )

        assert response.status_code == 201
        assert backend.oauth.exchange.call_args.kwargs["code_verifier"] == (
            generated_verifier(backend)
        )

    def test_expired_sign_in_is_rejected_before_exchange(
        self, client, backend, mock_session_store, redis_server
    ):
        # Without its record the sign-in has no verifier to present, so the
        # callback must not reach the exchange at all.
        client.post("/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK})
        fakeredis.FakeRedis(server=redis_server).flushall()

        response = client.post(
            "/api/oauth/callback", json={"url": self.callback_url("server-state-1")}
        )

        self.assert_rejected(response, backend, mock_session_store)

    def test_client_state_completes_sign_in_despite_unrelated_sign_in_cookie(
        self, client, other_client, backend, mock_session_store
    ):
        # A native client that also keeps cookies may hold a cookie from a
        # different sign-in; its own state still completes this one.
        client.post(
            "/api/oauth/authorize",
            json={"redirectUri": MOBILE_REDIRECT, "state": "client-state"},
        )
        other_client.post("/api/oauth/authorize", json={"redirectUri": MOBILE_REDIRECT})

        response = other_client.post(
            "/api/oauth/callback",
            json={
                "url": self.callback_url("server-state-1", base=MOBILE_REDIRECT),
                "state": "client-state",
            },
        )

        assert response.status_code == 201

    def test_native_callback_with_different_client_state_is_rejected(
        self, client, other_client, backend, mock_session_store
    ):
        client.post(
            "/api/oauth/authorize",
            json={"redirectUri": MOBILE_REDIRECT, "state": "client-state"},
        )

        response = other_client.post(
            "/api/oauth/callback",
            json={
                "url": self.callback_url("server-state-1", base=MOBILE_REDIRECT),
                "state": "another-client-state",
                "codeVerifier": "verifier",
            },
        )

        self.assert_rejected(response, backend, mock_session_store)

    def test_native_sign_in_cannot_be_completed_without_its_client_state(
        self, client, other_client, backend, mock_session_store
    ):
        client.post(
            "/api/oauth/authorize",
            json={"redirectUri": MOBILE_REDIRECT, "state": "client-state"},
        )

        response = other_client.post(
            "/api/oauth/callback",
            json={"url": self.callback_url("server-state-1", base=MOBILE_REDIRECT)},
        )

        self.assert_rejected(response, backend, mock_session_store)

    def test_client_state_for_one_sign_in_does_not_complete_another(
        self, client, other_client, backend, mock_session_store
    ):
        # Two native sign-ins; the state recorded for the first does not
        # vouch for the second.
        client.post(
            "/api/oauth/authorize",
            json={"redirectUri": MOBILE_REDIRECT, "state": "client-state-1"},
        )
        client.post(
            "/api/oauth/authorize",
            json={"redirectUri": MOBILE_REDIRECT, "state": "client-state-2"},
        )

        response = other_client.post(
            "/api/oauth/callback",
            json={
                "url": self.callback_url("server-state-2", base=MOBILE_REDIRECT),
                "state": "client-state-1",
            },
        )

        self.assert_rejected(response, backend, mock_session_store)
