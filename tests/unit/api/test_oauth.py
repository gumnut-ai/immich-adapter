"""Unit tests for OAuth API functions."""

from unittest.mock import Mock
from fastapi import Request
from starlette.datastructures import URL

from routers.api.oauth import rewrite_redirect_uri


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
            "x-forwarded-host": "adapter.gumnut.com",
        }.get(key)

        # Mock url_for to return a real URL object (simulating internal http URL)
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should use proxy headers to build the URL - scheme and host from headers
        assert result == "https://adapter.gumnut.com/api/oauth/mobile-redirect"
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
            "x-forwarded-host": "adapter.gumnut.com, proxy.internal",  # Multiple values
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
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
            "x-forwarded-host": " adapter.gumnut.com ",
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should strip whitespace from headers
        assert result == "https://adapter.gumnut.com/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_with_invalid_scheme_falls_back(self):
        """Test that invalid scheme in X-Forwarded-Proto falls back to url_for."""
        # Create a mock request with invalid scheme
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-proto": "ftp",  # Invalid scheme
            "x-forwarded-host": "adapter.gumnut.com",
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should fall back to url_for since scheme is invalid
        assert result == "http://localhost:3001/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_with_missing_proto_header(self):
        """Test that missing X-Forwarded-Proto falls back to url_for."""
        # Create a mock request with only host header
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-host": "adapter.gumnut.com",
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should fall back to url_for
        assert result == "http://localhost:3001/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_with_missing_host_header(self):
        """Test that missing X-Forwarded-Host falls back to url_for."""
        # Create a mock request with only proto header
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-proto": "https",
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should fall back to url_for
        assert result == "http://localhost:3001/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_with_empty_host_header(self):
        """Test that empty X-Forwarded-Host falls back to url_for."""
        # Create a mock request with empty host header
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "",  # Empty
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should fall back to url_for
        assert result == "http://localhost:3001/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_with_port_in_host(self):
        """Test mobile redirect with port number in X-Forwarded-Host."""
        # Create a mock request with port in host
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "adapter.gumnut.com:8443",
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should preserve port number from X-Forwarded-Host header
        assert result == "https://adapter.gumnut.com:8443/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")

    def test_mobile_redirect_case_sensitivity(self):
        """Test that scheme comparison is case-insensitive."""
        # Create a mock request with uppercase scheme
        request = Mock(spec=Request)
        request.headers.get.side_effect = lambda key: {
            "x-forwarded-proto": "HTTPS",  # Uppercase
            "x-forwarded-host": "adapter.gumnut.com",
        }.get(key)

        # Mock url_for to return a real URL object
        base_url = URL("http://localhost:3001/api/oauth/mobile-redirect")
        request.url_for.return_value = base_url

        result = rewrite_redirect_uri("app.immich:///oauth-callback", request)

        # Should normalize to lowercase scheme
        assert result == "https://adapter.gumnut.com/api/oauth/mobile-redirect"
        request.url_for.assert_called_once_with("redirect_oauth_to_mobile")
