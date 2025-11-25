import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from routers.utils.cookies import (
    AuthType,
    ImmichCookie,
    set_auth_cookies,
    update_access_token_cookie,
)


@pytest.fixture
def app():
    """Create a test FastAPI app."""
    app = FastAPI()

    @app.get("/test/set-auth-cookies")
    def test_set_auth_cookies(response: Response):
        """Endpoint to test set_auth_cookies."""
        set_auth_cookies(response, "test-token-123", AuthType.OAUTH)
        return {"status": "ok"}

    @app.get("/test/set-auth-cookies-with-secure")
    def test_set_auth_cookies_with_secure(request: Request, response: Response):
        """Endpoint to test set_auth_cookies with secure flag."""
        set_auth_cookies(response, "test-token-456", AuthType.PASSWORD, False)
        return {"status": "ok"}

    @app.get("/test/update-token")
    def test_update_token(response: Response):
        """Endpoint to test update_access_token_cookie."""
        update_access_token_cookie(response, "refreshed-token-789")
        return {"status": "ok"}

    @app.get("/test/update-token-with-secure")
    def test_update_token_with_secure(request: Request, response: Response):
        """Endpoint to test update_access_token_cookie with secure flag."""
        update_access_token_cookie(response, "refreshed-token-abc", False)
        return {"status": "ok"}

    return app


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


class TestSetAuthCookies:
    """Test cases for set_auth_cookies function."""

    def test_sets_all_three_cookies(self, client):
        """Test that set_auth_cookies sets all three required cookies."""
        response = client.get("/test/set-auth-cookies")

        assert response.status_code == 200
        # Check all three cookies are present
        assert ImmichCookie.ACCESS_TOKEN.value in response.cookies
        assert ImmichCookie.AUTH_TYPE.value in response.cookies
        assert ImmichCookie.IS_AUTHENTICATED.value in response.cookies

    def test_access_token_cookie_value(self, client):
        """Test that access token cookie has correct value."""
        response = client.get("/test/set-auth-cookies")

        assert response.cookies[ImmichCookie.ACCESS_TOKEN.value] == "test-token-123"

    def test_auth_type_cookie_value(self, client):
        """Test that auth type cookie has correct value."""
        response = client.get("/test/set-auth-cookies")

        assert response.cookies[ImmichCookie.AUTH_TYPE.value] == "oauth"

    def test_is_authenticated_cookie_value(self, client):
        """Test that is_authenticated cookie has correct value."""
        response = client.get("/test/set-auth-cookies")

        assert response.cookies[ImmichCookie.IS_AUTHENTICATED.value] == "true"

    def test_access_token_cookie_security_flags(self, client):
        """Test that access token cookie has correct security flags."""
        response = client.get("/test/set-auth-cookies")

        set_cookie_header = response.headers.get("set-cookie", "")
        # Access token should have HttpOnly flag
        assert "HttpOnly" in set_cookie_header
        assert ImmichCookie.ACCESS_TOKEN.value in set_cookie_header

    def test_different_auth_types(self, client):
        """Test setting cookies with different auth types."""
        response = client.get("/test/set-auth-cookies-with-secure")

        assert response.cookies[ImmichCookie.AUTH_TYPE.value] == "password"
        assert response.cookies[ImmichCookie.ACCESS_TOKEN.value] == "test-token-456"
        # Access token should have secure flag set to False
        set_cookie_header = response.headers.get("set-cookie", "")
        assert "Secure" not in set_cookie_header


class TestUpdateAccessTokenCookie:
    """Test cases for update_access_token_cookie function."""

    def test_updates_only_access_token(self, client):
        """Test that update_access_token_cookie only updates access token."""
        response = client.get("/test/update-token")

        assert response.status_code == 200
        # Only access token should be present
        assert ImmichCookie.ACCESS_TOKEN.value in response.cookies
        # Auth type and is_authenticated should NOT be set
        assert ImmichCookie.AUTH_TYPE.value not in response.cookies
        assert ImmichCookie.IS_AUTHENTICATED.value not in response.cookies

    def test_refreshed_token_value(self, client):
        """Test that refreshed token has correct value."""
        response = client.get("/test/update-token")

        assert (
            response.cookies[ImmichCookie.ACCESS_TOKEN.value] == "refreshed-token-789"
        )

    def test_refreshed_token_security_flags(self, client):
        """Test that refreshed token has correct security flags."""
        response = client.get("/test/update-token")

        set_cookie_header = response.headers.get("set-cookie", "")
        # Should have HttpOnly flag
        assert "HttpOnly" in set_cookie_header
        assert ImmichCookie.ACCESS_TOKEN.value in set_cookie_header

    def test_with_request_object(self, client):
        """Test that update works correctly when secure is provided."""
        response = client.get("/test/update-token-with-secure")

        assert (
            response.cookies[ImmichCookie.ACCESS_TOKEN.value] == "refreshed-token-abc"
        )
        # Access token should have secure flag set to False
        set_cookie_header = response.headers.get("set-cookie", "")
        assert "Secure" not in set_cookie_header
