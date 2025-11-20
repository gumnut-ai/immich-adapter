import pytest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

from main import app
from routers.utils.cookies import ImmichCookie
from routers.utils.gumnut_client import get_unauthenticated_gumnut_client


class TestOAuthCookieSecurity:
    """Test that OAuth cookies have proper security flags."""

    @pytest.mark.anyio
    async def test_oauth_callback_cookies_have_security_flags(self):
        """Verify all cookies from OAuth callback have secure flags."""
        # Mock the Gumnut client
        mock_gumnut_client = Mock()
        mock_exchange_result = Mock()
        mock_exchange_result.access_token = "jwt_token_abc123"
        mock_exchange_result.user = Mock()
        mock_exchange_result.user.id = "user-uuid-123"
        mock_exchange_result.user.email = "test@example.com"
        mock_exchange_result.user.first_name = "Test"
        mock_exchange_result.user.last_name = "User"
        mock_gumnut_client.oauth.exchange.return_value = mock_exchange_result

        # Override the dependency
        async def mock_get_client():
            return mock_gumnut_client

        app.dependency_overrides[get_unauthenticated_gumnut_client] = mock_get_client

        try:
            with patch("routers.api.oauth.parse_callback_url") as mock_parse:
                mock_parse.return_value = {
                    "code": "abc123",
                    "state": "xyz789",
                    "error": None,
                }

                # Use TestClient to make actual request with HTTPS base URL
                client = TestClient(app, base_url="https://testserver")
                response = client.post(
                    "/api/oauth/callback",
                    json={
                        "url": "http://localhost:3000/auth/callback?code=abc123&state=xyz789"
                    },
                )

                # Assert response is successful
                assert response.status_code == 201
                assert response.json()["accessToken"] == "jwt_token_abc123"

                # Parse Set-Cookie headers from response
                set_cookie_headers = response.headers.get_list("set-cookie")

                # Should have 3 cookies: access_token, auth_type, is_authenticated
                assert len(set_cookie_headers) == 3

                # Check each cookie for security flags
                cookie_checks = {
                    ImmichCookie.ACCESS_TOKEN.value: False,
                    ImmichCookie.AUTH_TYPE.value: False,
                    ImmichCookie.IS_AUTHENTICATED.value: False,
                }

                for cookie_header in set_cookie_headers:
                    # Check for access_token cookie
                    if f"{ImmichCookie.ACCESS_TOKEN.value}=" in cookie_header:
                        assert "HttpOnly" in cookie_header, (
                            f"access_token missing HttpOnly: {cookie_header}"
                        )
                        assert "Secure" in cookie_header or "secure" in cookie_header, (
                            f"access_token missing Secure: {cookie_header}"
                        )
                        assert (
                            "SameSite=lax" in cookie_header
                            or "SameSite=Lax" in cookie_header
                        ), f"access_token missing SameSite=Lax: {cookie_header}"
                        cookie_checks[ImmichCookie.ACCESS_TOKEN.value] = True

                    # Check for auth_type cookie
                    elif f"{ImmichCookie.AUTH_TYPE.value}=" in cookie_header:
                        assert "HttpOnly" in cookie_header, (
                            f"auth_type missing HttpOnly: {cookie_header}"
                        )
                        assert "Secure" in cookie_header or "secure" in cookie_header, (
                            f"auth_type missing Secure: {cookie_header}"
                        )
                        assert (
                            "SameSite=lax" in cookie_header
                            or "SameSite=Lax" in cookie_header
                        ), f"auth_type missing SameSite=Lax: {cookie_header}"
                        cookie_checks[ImmichCookie.AUTH_TYPE.value] = True

                    # Check for is_authenticated cookie
                    elif f"{ImmichCookie.IS_AUTHENTICATED.value}=" in cookie_header:
                        assert "Secure" in cookie_header or "secure" in cookie_header, (
                            f"is_authenticated missing Secure: {cookie_header}"
                        )
                        assert (
                            "SameSite=lax" in cookie_header
                            or "SameSite=Lax" in cookie_header
                        ), f"is_authenticated missing SameSite=Lax: {cookie_header}"
                        # Note: is_authenticated doesn't have HttpOnly by design (may need to be accessible to JS)
                        cookie_checks[ImmichCookie.IS_AUTHENTICATED.value] = True

                # Verify all expected cookies were found
                for cookie_name, found in cookie_checks.items():
                    assert found, f"Cookie {cookie_name} was not set in response"
        finally:
            # Clean up dependency override
            app.dependency_overrides.clear()

    @pytest.mark.anyio
    async def test_token_refresh_cookie_has_security_flags(self):
        """Verify refreshed token cookie has security flags."""
        # Create test client with HTTPS base URL
        client = TestClient(app, base_url="https://testserver")

        # Set cookie on client instance instead of per-request
        client.cookies.set(ImmichCookie.ACCESS_TOKEN.value, "original-jwt-token-123")

        # Mock a request that would trigger token refresh
        with patch(
            "routers.utils.gumnut_client.get_refreshed_token"
        ) as mock_get_refreshed:
            mock_get_refreshed.return_value = "refreshed-jwt-token-789"

            # Make a request with existing auth cookie
            response = client.get("/api/server/about")

            # Should be successful
            assert response.status_code == 200

            # Check if refresh cookie was set (if middleware detected refresh)
            set_cookie_headers = response.headers.get_list("set-cookie")

            # If token was refreshed, verify security flags
            for cookie_header in set_cookie_headers:
                if f"{ImmichCookie.ACCESS_TOKEN.value}=" in cookie_header:
                    assert "HttpOnly" in cookie_header, (
                        f"refreshed token missing HttpOnly: {cookie_header}"
                    )
                    assert "Secure" in cookie_header or "secure" in cookie_header, (
                        f"refreshed token missing Secure: {cookie_header}"
                    )
                    assert (
                        "SameSite=lax" in cookie_header
                        or "SameSite=Lax" in cookie_header
                    ), f"refreshed token missing SameSite=Lax: {cookie_header}"

    @pytest.mark.anyio
    async def test_no_cookies_set_without_security_flags(self):
        """Ensure no cookies are set without proper security flags (negative test)."""
        # This test verifies that we're not accidentally setting cookies
        # through any other code path without security flags
        client = TestClient(app, base_url="https://testserver")

        # Make various requests and check that any cookies set have security flags
        test_endpoints = [
            ("/api/server/about", "GET"),
            ("/api/server/config", "GET"),
        ]

        for endpoint, method in test_endpoints:
            if method == "GET":
                response = client.get(endpoint)

            # If any cookies were set, verify they have security flags
            set_cookie_headers = response.headers.get_list("set-cookie")
            for cookie_header in set_cookie_headers:
                # Parse cookie name
                cookie_name = cookie_header.split("=")[0]

                # If it's an authentication-related cookie, it MUST have security flags
                if cookie_name in [
                    ImmichCookie.ACCESS_TOKEN.value,
                    ImmichCookie.AUTH_TYPE.value,
                    ImmichCookie.IS_AUTHENTICATED.value,
                ]:
                    # These cookies must have Secure flag
                    assert "Secure" in cookie_header or "secure" in cookie_header, (
                        f"Auth cookie {cookie_name} missing Secure flag on {endpoint}: {cookie_header}"
                    )

                    # These cookies must have SameSite flag
                    assert "SameSite" in cookie_header, (
                        f"Auth cookie {cookie_name} missing SameSite flag on {endpoint}: {cookie_header}"
                    )

                    # Access token and auth type should have HttpOnly
                    if cookie_name in [
                        ImmichCookie.ACCESS_TOKEN.value,
                        ImmichCookie.AUTH_TYPE.value,
                    ]:
                        assert "HttpOnly" in cookie_header, (
                            f"Auth cookie {cookie_name} missing HttpOnly flag on {endpoint}: {cookie_header}"
                        )
