"""Unit tests for AuthMiddleware with mocked session store."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID
import httpx

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from routers.middleware.auth_middleware import AuthMiddleware
from services.session_store import Session
from routers.utils.gumnut_client import _response_hook
from utils.jwt_encryption import JWTEncryptionError

# Test UUIDs for consistent testing
TEST_SESSION_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_ENCRYPTED_JWT = "gAAAAABh..."  # Mock encrypted JWT
TEST_JWT = "test.jwt.token"


def create_test_session(session_id: UUID = TEST_SESSION_ID) -> Session:
    """Create a test session."""
    now = datetime.now(timezone.utc)
    return Session(
        id=session_id,
        user_id="user_123",
        library_id="lib_456",
        stored_jwt=TEST_ENCRYPTED_JWT,
        device_type="iOS",
        device_os="iOS 17.4",
        app_version="1.94.0",
        created_at=now,
        updated_at=now,
        is_pending_sync_reset=False,
    )


@pytest.fixture
def mock_session_store():
    """Create a mock SessionStore."""
    store = AsyncMock()
    session = create_test_session()
    store.get_by_id.return_value = session
    store.update_stored_jwt.return_value = True
    return store


@pytest.fixture
def app_with_mocks(mock_session_store):
    """Create a test FastAPI app with mocked dependencies."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/test/protected")
    async def protected_endpoint(request: Request):
        """Test endpoint that returns auth info from request state."""
        return {
            "jwt_token": getattr(request.state, "jwt_token", None),
            "session_token": getattr(request.state, "session_token", None),
            "is_web_client": getattr(request.state, "is_web_client", None),
        }

    @app.get("/api/oauth/login")
    async def unauthenticated_endpoint():
        """Test endpoint that should bypass auth."""
        return {"message": "login page"}

    return app


@pytest.fixture
def client_with_mocks(app_with_mocks, mock_session_store):
    """Create a test client with mocked session store."""

    # Mock get_session_store to return our mock
    async def mock_get_session_store():
        return mock_session_store

    with patch(
        "routers.middleware.auth_middleware.get_session_store",
        mock_get_session_store,
    ):
        # Also mock the session's get_jwt method
        session = mock_session_store.get_by_id.return_value
        session.get_jwt = MagicMock(return_value=TEST_JWT)

        yield TestClient(app_with_mocks)


class TestAuthMiddleware:
    """Test cases for AuthMiddleware with session lookup."""

    def test_non_api_paths_bypass_auth_entirely(self, mock_session_store):
        """Test that non-API paths (static files, SPA routes) bypass auth middleware entirely."""
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/photos")
        async def spa_route():
            """Simulates an SPA route that should bypass auth."""
            return {"page": "photos"}

        mock_get_session_store = AsyncMock(return_value=mock_session_store)

        with patch(
            "routers.middleware.auth_middleware.get_session_store",
            mock_get_session_store,
        ):
            client = TestClient(app)

            # Request with an invalid session token - should still succeed
            # because non-API paths bypass auth entirely
            headers = {"Authorization": "Bearer invalid-session-token"}
            response = client.get("/photos", headers=headers)

            assert response.status_code == 200
            assert response.json() == {"page": "photos"}
            # get_session_store should NOT be called for non-API paths
            mock_get_session_store.assert_not_awaited()

    def test_mobile_client_with_bearer_token(
        self, client_with_mocks, mock_session_store
    ):
        """Test that mobile client with Bearer token looks up session."""
        session_token = str(TEST_SESSION_ID)
        headers = {"Authorization": f"Bearer {session_token}"}

        response = client_with_mocks.get("/api/test/protected", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["jwt_token"] == TEST_JWT
        assert data["session_token"] == session_token
        assert data["is_web_client"] is False

    def test_mobile_client_with_immich_user_token(
        self, client_with_mocks, mock_session_store
    ):
        """Test that mobile client with x-immich-user-token header looks up session."""
        session_token = str(TEST_SESSION_ID)
        headers = {"x-immich-user-token": session_token}

        response = client_with_mocks.get("/api/test/protected", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["jwt_token"] == TEST_JWT
        assert data["session_token"] == session_token
        assert data["is_web_client"] is False

    def test_web_client_with_cookie(self, client_with_mocks, mock_session_store):
        """Test that web client with cookie looks up session."""
        session_token = str(TEST_SESSION_ID)
        client_with_mocks.cookies = {"immich_access_token": session_token}

        response = client_with_mocks.get("/api/test/protected")

        assert response.status_code == 200
        data = response.json()
        assert data["jwt_token"] == TEST_JWT
        assert data["session_token"] == session_token
        assert data["is_web_client"] is True

    def test_no_authentication(self, client_with_mocks, mock_session_store):
        """Test request with no authentication."""
        response = client_with_mocks.get("/api/test/protected")

        assert response.status_code == 200
        data = response.json()
        assert data["jwt_token"] is None
        assert data["session_token"] is None
        assert data["is_web_client"] is False

    def test_session_not_found(self, client_with_mocks, mock_session_store):
        """Test that missing session returns 401."""
        mock_session_store.get_by_id.return_value = None
        headers = {"Authorization": "Bearer nonexistent-session"}

        response = client_with_mocks.get("/api/test/protected", headers=headers)

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid user token"

    def test_bearer_takes_precedence_over_cookie(
        self, client_with_mocks, mock_session_store
    ):
        """Test that Authorization header takes precedence over cookie."""
        session_token = str(TEST_SESSION_ID)
        headers = {"Authorization": f"Bearer {session_token}"}
        client_with_mocks.cookies = {"immich_access_token": "cookie-session-token"}

        response = client_with_mocks.get("/api/test/protected", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["session_token"] == session_token
        assert data["is_web_client"] is False

    def test_bearer_takes_precedence_over_immich_user_token(
        self, client_with_mocks, mock_session_store
    ):
        """Test that Authorization Bearer header takes precedence over x-immich-user-token."""
        session_token = str(TEST_SESSION_ID)
        headers = {
            "Authorization": f"Bearer {session_token}",
            "x-immich-user-token": "immich-token-456",
        }

        response = client_with_mocks.get("/api/test/protected", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["session_token"] == session_token
        assert data["is_web_client"] is False

    def test_immich_user_token_takes_precedence_over_cookie(
        self, client_with_mocks, mock_session_store
    ):
        """Test that x-immich-user-token header takes precedence over cookie."""
        session_token = str(TEST_SESSION_ID)
        headers = {"x-immich-user-token": session_token}
        client_with_mocks.cookies = {"immich_access_token": "cookie-session-token"}

        response = client_with_mocks.get("/api/test/protected", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["session_token"] == session_token
        assert data["is_web_client"] is False

    def test_unauthenticated_paths_bypass_auth(
        self, client_with_mocks, mock_session_store
    ):
        """Test that unauthenticated paths bypass authentication middleware."""
        response = client_with_mocks.get("/api/oauth/login")

        assert response.status_code == 200
        assert response.json() == {"message": "login page"}


class TestTokenRefresh:
    """Test cases for JWT refresh handling."""

    @pytest.fixture
    def app_with_refresh(self):
        """Create a test FastAPI app with refresh endpoint."""
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/api/test/refresh")
        async def refresh_endpoint(request: Request):
            """Test endpoint that simulates token refresh from Gumnut backend."""

            # Simulate what happens when Gumnut backend returns a refresh header
            mock_response = httpx.Response(
                status_code=200,
                headers={"x-new-access-token": "new-jwt-token-123"},
                json={"status": "ok"},
            )
            _response_hook(mock_response)

            return {"status": "ok"}

        return app

    @pytest.fixture
    def client_with_refresh(self, app_with_refresh, mock_session_store):
        """Create a test client for refresh tests."""

        # Mock get_session_store to return our mock
        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "routers.middleware.auth_middleware.get_session_store",
            mock_get_session_store,
        ):
            session = mock_session_store.get_by_id.return_value
            session.get_jwt = MagicMock(return_value=TEST_JWT)
            yield TestClient(app_with_refresh)

    def test_token_refresh_updates_session(
        self, client_with_refresh, mock_session_store
    ):
        """Test that JWT refresh updates the stored JWT in the session."""
        session_token = str(TEST_SESSION_ID)
        client_with_refresh.cookies = {"immich_access_token": session_token}

        response = client_with_refresh.get("/api/test/refresh")

        assert response.status_code == 200
        # Verify update_stored_jwt was called with the new token
        mock_session_store.update_stored_jwt.assert_called_once_with(
            session_token, "new-jwt-token-123"
        )
        # Verify refresh header is stripped (client doesn't need it)
        assert "x-new-access-token" not in response.headers

    def test_token_refresh_for_mobile_client(
        self, client_with_refresh, mock_session_store
    ):
        """Test JWT refresh for mobile client."""
        session_token = str(TEST_SESSION_ID)
        headers = {"Authorization": f"Bearer {session_token}"}

        response = client_with_refresh.get("/api/test/refresh", headers=headers)

        assert response.status_code == 200
        # Verify update_stored_jwt was called
        mock_session_store.update_stored_jwt.assert_called_once_with(
            session_token, "new-jwt-token-123"
        )
        # Refresh header should be stripped for all clients now
        assert "x-new-access-token" not in response.headers


class TestSessionLookupErrors:
    """Test cases for session lookup error handling."""

    @pytest.fixture
    def app_for_errors(self):
        """Create a test FastAPI app."""
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/api/test/protected")
        async def protected_endpoint(request: Request):
            return {
                "jwt_token": getattr(request.state, "jwt_token", None),
                "session_token": getattr(request.state, "session_token", None),
            }

        return app

    def test_jwt_decryption_error_returns_500(self, app_for_errors):
        """Test that JWT decryption errors return 500."""

        mock_session_store = AsyncMock()
        session = create_test_session()
        session.get_jwt = MagicMock(side_effect=JWTEncryptionError("decryption failed"))
        mock_session_store.get_by_id.return_value = session

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "routers.middleware.auth_middleware.get_session_store",
            mock_get_session_store,
        ):
            client = TestClient(app_for_errors)
            session_token = str(TEST_SESSION_ID)
            headers = {"Authorization": f"Bearer {session_token}"}

            response = client.get("/api/test/protected", headers=headers)

            assert response.status_code == 500
            assert response.json()["detail"] == "Internal server error"

    def test_redis_error_returns_500(self, app_for_errors):
        """Test that Redis errors return 500."""
        mock_session_store = AsyncMock()
        mock_session_store.get_by_id.side_effect = Exception("Redis connection error")

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "routers.middleware.auth_middleware.get_session_store",
            mock_get_session_store,
        ):
            client = TestClient(app_for_errors)
            session_token = str(TEST_SESSION_ID)
            headers = {"Authorization": f"Bearer {session_token}"}

            response = client.get("/api/test/protected", headers=headers)

            assert response.status_code == 500
            assert response.json()["detail"] == "Internal server error"
