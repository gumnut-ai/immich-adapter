import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from routers.utils.cookies import (
    COOKIE_MAX_AGE_SECONDS,
    AuthType,
    ImmichCookie,
    set_auth_cookies,
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
    def test_set_auth_cookies_with_secure(response: Response):
        """Endpoint to test set_auth_cookies with secure flag."""
        set_auth_cookies(response, "test-token-456", AuthType.PASSWORD, False)
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

    @pytest.mark.parametrize(
        "endpoint",
        ["/test/set-auth-cookies", "/test/set-auth-cookies-with-secure"],
    )
    def test_all_cookies_have_max_age(self, client, endpoint):
        """All three auth cookies must include Max-Age so iOS persists them
        across app process death — on both the secure=True and secure=False
        paths, so a future conditional on `secure` can't drop Max-Age."""
        response = client.get(endpoint)

        cookie_headers = response.headers.get_list("set-cookie")
        expected = f"Max-Age={COOKIE_MAX_AGE_SECONDS}"
        for cookie_name in (
            ImmichCookie.ACCESS_TOKEN.value,
            ImmichCookie.AUTH_TYPE.value,
            ImmichCookie.IS_AUTHENTICATED.value,
        ):
            matching = [h for h in cookie_headers if h.startswith(f"{cookie_name}=")]
            assert matching, f"No Set-Cookie header for {cookie_name}"
            assert expected in matching[0], (
                f"{cookie_name} missing {expected}: {matching[0]}"
            )
