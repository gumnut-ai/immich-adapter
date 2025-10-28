import pytest
import httpx
from unittest.mock import Mock
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routers.middleware.auth_middleware import AuthMiddleware
from routers.utils.gumnut_client import (
    _response_hook,
    get_refreshed_token,
    clear_refreshed_token,
    get_shared_http_client,
)


class TestRefreshTokenHook:
    """Test the response hook that captures refresh tokens."""

    def test_capture_refresh_token_hook_with_header(self):
        """Test that the hook captures x-new-access-token header."""
        # Setup
        mock_response = Mock()
        mock_response.headers = {"x-new-access-token": "new-token-123"}

        # Clear any previous token
        clear_refreshed_token()

        # Execute
        _response_hook(mock_response)

        # Assert
        assert get_refreshed_token() == "new-token-123"

    def test_capture_refresh_token_hook_without_header(self):
        """Test that the hook does nothing when header is absent."""
        # Setup
        mock_response = Mock()
        mock_response.headers = {"content-type": "application/json"}

        # Clear and verify
        clear_refreshed_token()
        assert get_refreshed_token() is None

        # Execute
        _response_hook(mock_response)

        # Assert - should still be None
        assert get_refreshed_token() is None

    def test_clear_refreshed_token(self):
        """Test that clear_refreshed_token works correctly."""
        # Setup - set a token
        mock_response = Mock()
        mock_response.headers = {"x-new-access-token": "token-to-clear"}
        _response_hook(mock_response)
        assert get_refreshed_token() == "token-to-clear"

        # Execute
        clear_refreshed_token()

        # Assert
        assert get_refreshed_token() is None


class TestTokenRefreshIntegration:
    """Integration tests for token refresh through the full stack."""

    @pytest.fixture
    def app(self):
        """Create a test FastAPI app with AuthMiddleware."""
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/api/test/albums")
        async def test_endpoint(request: Request):
            """
            Test endpoint that simulates calling Gumnut backend.

            In real usage, this would call the Gumnut SDK which would
            trigger the response hook if the backend returns a refresh header.
            """
            # Simulate what happens when Gumnut backend returns a refresh header
            # by calling the hook directly
            mock_response = httpx.Response(
                status_code=200,
                headers={"x-new-access-token": "refreshed-jwt-456"},
                json={"albums": []},
            )
            _response_hook(mock_response)

            return {"albums": []}

        return app

    @pytest.fixture
    def client(self, app):
        """Create a test client."""
        return TestClient(app)

    def test_token_refresh_for_web_client(self, client):
        """Test that web client receives updated cookie when token is refreshed."""
        # Setup - web client with cookie
        client.cookies.set("immich_access_token", "old-token-123")

        # Execute
        response = client.get("/api/test/albums")

        # Assert
        assert response.status_code == 200
        # Check that cookie was updated with refreshed token
        # TestClient returns cookies in set-cookie header
        set_cookie_header = response.headers.get("set-cookie", "")
        assert "immich_access_token=refreshed-jwt-456" in set_cookie_header
        assert "HttpOnly" in set_cookie_header
        # Check that x-new-access-token header is NOT in response (web client)
        assert "x-new-access-token" not in response.headers

    def test_token_refresh_for_mobile_client(self, client):
        """Test that mobile client receives header when token is refreshed."""
        # Setup - mobile client with Authorization header
        headers = {"Authorization": "Bearer old-mobile-token"}

        # Execute
        response = client.get("/api/test/albums", headers=headers)

        # Assert
        assert response.status_code == 200
        # Check that x-new-access-token header is in response
        assert "x-new-access-token" in response.headers
        assert response.headers["x-new-access-token"] == "refreshed-jwt-456"
        # Check that no cookie was set (mobile client)
        set_cookie_header = response.headers.get("set-cookie", "")
        assert "immich_access_token" not in set_cookie_header

    def test_no_token_refresh_when_backend_doesnt_refresh(self, client):
        """Test normal flow when backend doesn't refresh token."""

        @pytest.fixture
        def app_no_refresh(self):
            """App that doesn't trigger token refresh."""
            app = FastAPI()
            app.add_middleware(AuthMiddleware)

            @app.get("/api/test/normal")
            async def normal_endpoint(request: Request):
                # Don't set any refreshed token
                return {"data": "ok"}

            return app

        # Create client for this specific test
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/api/test/normal")
        async def normal_endpoint(request: Request):
            # Don't set any refreshed token - simulate no refresh from backend
            return {"data": "ok"}

        test_client = TestClient(app)
        test_client.cookies.set("immich_access_token", "original-token")

        # Execute
        response = test_client.get("/api/test/normal")

        # Assert
        assert response.status_code == 200
        # No new cookie should be set
        assert "immich_access_token" not in response.cookies
        # No refresh header
        assert "x-new-access-token" not in response.headers


class TestTokenRefreshWithMockedGumnut:
    """Test token refresh with mocked Gumnut SDK responses."""

    def test_gumnut_response_with_refresh_header(self):
        """Test that httpx response hook captures refresh header from Gumnut."""
        # Clear any previous token
        clear_refreshed_token()

        # Create a mock response that includes refresh header
        mock_response = httpx.Response(
            status_code=200,
            headers={"x-new-access-token": "backend-refreshed-token"},
            json={"albums": []},
        )

        # Get the httpx client (which has our hook registered)
        client = get_shared_http_client()

        # Simulate the hook being called (as httpx would do)
        for hook in client.event_hooks["response"]:
            hook(mock_response)

        # Assert that the token was captured
        assert get_refreshed_token() == "backend-refreshed-token"

    def test_multiple_requests_dont_interfere(self):
        """Test that clearing tokens between requests prevents interference."""
        client = get_shared_http_client()

        # First request with refresh
        clear_refreshed_token()
        response1 = httpx.Response(
            status_code=200,
            headers={"x-new-access-token": "token-1"},
            json={},
        )
        for hook in client.event_hooks["response"]:
            hook(response1)
        assert get_refreshed_token() == "token-1"

        # Second request without refresh (after clearing)
        clear_refreshed_token()
        response2 = httpx.Response(
            status_code=200,
            headers={},
            json={},
        )
        for hook in client.event_hooks["response"]:
            hook(response2)
        assert get_refreshed_token() is None

        # Third request with different refresh token
        clear_refreshed_token()
        response3 = httpx.Response(
            status_code=200,
            headers={"x-new-access-token": "token-3"},
            json={},
        )
        for hook in client.event_hooks["response"]:
            hook(response3)
        assert get_refreshed_token() == "token-3"
