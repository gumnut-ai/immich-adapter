import pytest
from routers.utils.oauth_utils import parse_callback_url, normalize_redirect_uri


class TestNormalizeRedirectUri:
    """Test the normalize_redirect_uri function."""

    def test_basic_urls_unchanged(self):
        """Test that well-formed URLs without special cases remain unchanged."""
        assert (
            normalize_redirect_uri("http://localhost:3000/auth/callback")
            == "http://localhost:3000/auth/callback"
        )
        assert (
            normalize_redirect_uri("https://app.example.com/oauth/callback")
            == "https://app.example.com/oauth/callback"
        )

    def test_trailing_slashes_removed(self):
        """Test that trailing slashes are stripped from paths."""
        assert (
            normalize_redirect_uri("http://localhost:3000/auth/callback/")
            == "http://localhost:3000/auth/callback"
        )
        assert (
            normalize_redirect_uri("https://app.example.com/oauth/callback///")
            == "https://app.example.com/oauth/callback"
        )

    def test_default_ports_stripped(self):
        """Test that default ports (80 for HTTP, 443 for HTTPS) are removed."""
        assert (
            normalize_redirect_uri("http://localhost:80/auth/callback")
            == "http://localhost/auth/callback"
        )
        assert (
            normalize_redirect_uri("https://app.example.com:443/oauth/callback")
            == "https://app.example.com/oauth/callback"
        )

    def test_non_default_ports_preserved(self):
        """Test that non-default ports are kept."""
        assert (
            normalize_redirect_uri("http://localhost:8080/auth/callback")
            == "http://localhost:8080/auth/callback"
        )
        assert (
            normalize_redirect_uri("https://app.example.com:8443/oauth/callback")
            == "https://app.example.com:8443/oauth/callback"
        )

    def test_case_normalization(self):
        """Test that scheme and hostname are lowercased, path remains case-sensitive."""
        assert (
            normalize_redirect_uri("HTTP://LocalHost:3000/Auth/Callback")
            == "http://localhost:3000/Auth/Callback"
        )
        assert (
            normalize_redirect_uri("HTTPS://App.Example.COM/OAuth/Callback")
            == "https://app.example.com/OAuth/Callback"
        )

    def test_query_parameters_preserved(self):
        """Test that query parameters are preserved (allowed in OAuth redirect URIs)."""
        assert (
            normalize_redirect_uri("http://localhost:3000/callback?state=abc123")
            == "http://localhost:3000/callback?state=abc123"
        )
        assert (
            normalize_redirect_uri(
                "https://app.example.com/oauth?client_id=test&state=xyz"
            )
            == "https://app.example.com/oauth?client_id=test&state=xyz"
        )

    def test_fragments_stripped(self):
        """Test that fragments are stripped (not allowed per OAuth 2.0 spec)."""
        assert (
            normalize_redirect_uri("http://localhost:3000/callback#fragment")
            == "http://localhost:3000/callback"
        )
        assert (
            normalize_redirect_uri("https://app.example.com/oauth#section")
            == "https://app.example.com/oauth"
        )

    def test_query_parameters_preserved_fragments_stripped(self):
        """Test that query parameters are kept but fragments are stripped."""
        assert (
            normalize_redirect_uri("http://localhost:3000/callback?state=abc#fragment")
            == "http://localhost:3000/callback?state=abc"
        )
        assert (
            normalize_redirect_uri(
                "https://app.example.com/oauth?client_id=test&state=xyz#section"
            )
            == "https://app.example.com/oauth?client_id=test&state=xyz"
        )

    def test_root_path_normalization(self):
        """Test that root paths are normalized to single '/'."""
        assert (
            normalize_redirect_uri("http://localhost:3000") == "http://localhost:3000/"
        )
        assert (
            normalize_redirect_uri("http://localhost:3000/") == "http://localhost:3000/"
        )
        assert (
            normalize_redirect_uri("http://localhost:3000///")
            == "http://localhost:3000/"
        )

    def test_root_path_with_query(self):
        """Test root path with query parameters."""
        assert (
            normalize_redirect_uri("http://localhost:3000?state=abc")
            == "http://localhost:3000/?state=abc"
        )
        assert (
            normalize_redirect_uri("http://localhost:3000/?state=abc")
            == "http://localhost:3000/?state=abc"
        )

    def test_complex_normalization(self):
        """Test complex case with multiple transformations applied."""
        assert (
            normalize_redirect_uri(
                "HTTP://LocalHost:80/Auth/Callback/?state=xyz&client=test#fragment"
            )
            == "http://localhost/Auth/Callback?state=xyz&client=test"
        )

    def test_url_encoded_query_parameters(self):
        """Test that URL-encoded query parameters are preserved."""
        assert (
            normalize_redirect_uri(
                "http://localhost:3000/callback?redirect_uri=http%3A%2F%2Fexample.com"
            )
            == "http://localhost:3000/callback?redirect_uri=http%3A%2F%2Fexample.com"
        )

    def test_multiple_query_parameters(self):
        """Test URLs with multiple query parameters."""
        assert (
            normalize_redirect_uri(
                "http://localhost:3000/callback?client_id=123&state=abc&scope=read"
            )
            == "http://localhost:3000/callback?client_id=123&state=abc&scope=read"
        )

    def test_equivalence_for_security_comparison(self):
        """Test that equivalent URIs normalize to the same value for security checks."""
        # These should all normalize to the same value (except query params differ)
        uri1 = "HTTP://LocalHost:80/auth/callback/#fragment"
        uri2 = "http://localhost/auth/callback"
        uri3 = "http://LOCALHOST:80/auth/callback/"

        assert normalize_redirect_uri(uri1) == normalize_redirect_uri(uri2)
        assert normalize_redirect_uri(uri2) == normalize_redirect_uri(uri3)

    def test_different_query_params_not_equivalent(self):
        """Test that URIs with different query params are NOT considered equivalent."""
        uri1 = "http://localhost:3000/callback?state=abc"
        uri2 = "http://localhost:3000/callback?state=xyz"

        assert normalize_redirect_uri(uri1) != normalize_redirect_uri(uri2)

    def test_query_param_order_preserved(self):
        """Test that query parameter order is preserved (not reordered)."""
        uri = "http://localhost:3000/callback?z=1&a=2&m=3"
        # URL normalization preserves order, doesn't alphabetize
        assert (
            normalize_redirect_uri(uri) == "http://localhost:3000/callback?z=1&a=2&m=3"
        )

    def test_empty_query_parameter(self):
        """Test handling of empty query parameter values."""
        assert (
            normalize_redirect_uri("http://localhost:3000/callback?state=")
            == "http://localhost:3000/callback?state="
        )


class TestParseCallbackUrl:
    """Test the parse_callback_url function."""

    def test_parse_callback_url_success(self):
        """Test successful parsing of OAuth callback URL with code and state."""
        url = "http://localhost:3000/auth/callback?code=abc123&state=xyz789"

        result = parse_callback_url(url)

        assert result["code"] == "abc123"
        assert result["state"] == "xyz789"
        assert result["error"] is None

    def test_parse_callback_url_with_error(self):
        """Test parsing callback URL with OAuth error."""
        url = "http://localhost:3000/auth/callback?error=access_denied&state=xyz789"

        result = parse_callback_url(url)

        assert result["code"] is None
        assert result["state"] == "xyz789"
        assert result["error"] == "access_denied"

    def test_parse_callback_url_with_error_and_code(self):
        """Test parsing callback URL with both error and code (error takes precedence)."""
        url = "http://localhost:3000/auth/callback?code=abc123&error=server_error&state=xyz789"

        result = parse_callback_url(url)

        assert result["code"] == "abc123"
        assert result["state"] == "xyz789"
        assert result["error"] == "server_error"

    def test_parse_callback_url_missing_state(self):
        """Test that missing state parameter raises ValueError."""
        url = "http://localhost:3000/auth/callback?code=abc123"

        with pytest.raises(ValueError) as exc_info:
            parse_callback_url(url)

        assert "Missing required 'state' parameter" in str(exc_info.value)

    def test_parse_callback_url_empty_state(self):
        """Test that empty state parameter raises ValueError."""
        url = "http://localhost:3000/auth/callback?code=abc123&state="

        with pytest.raises(ValueError) as exc_info:
            parse_callback_url(url)

        assert "Missing required 'state' parameter" in str(exc_info.value)

    def test_parse_callback_url_with_additional_params(self):
        """Test parsing URL with additional query parameters (should be ignored)."""
        url = "http://localhost:3000/auth/callback?code=abc123&state=xyz789&extra=param&foo=bar"

        result = parse_callback_url(url)

        assert result["code"] == "abc123"
        assert result["state"] == "xyz789"
        assert result["error"] is None

    def test_parse_callback_url_url_encoded_values(self):
        """Test parsing URL with URL-encoded parameter values."""
        url = "http://localhost:3000/auth/callback?code=abc%20123&state=xyz%2B789"

        result = parse_callback_url(url)

        assert result["code"] == "abc 123"
        assert result["state"] == "xyz+789"
        assert result["error"] is None

    def test_parse_callback_url_with_fragment(self):
        """Test parsing URL with fragment identifier (should be ignored)."""
        url = "http://localhost:3000/auth/callback?code=abc123&state=xyz789#fragment"

        result = parse_callback_url(url)

        assert result["code"] == "abc123"
        assert result["state"] == "xyz789"
        assert result["error"] is None

    def test_parse_callback_url_https(self):
        """Test parsing HTTPS callback URL."""
        url = "https://example.com/auth/callback?code=abc123&state=xyz789"

        result = parse_callback_url(url)

        assert result["code"] == "abc123"
        assert result["state"] == "xyz789"
        assert result["error"] is None

    def test_parse_callback_url_different_path(self):
        """Test parsing callback URL with different path."""
        url = "http://localhost:3000/oauth/callback?code=abc123&state=xyz789"

        result = parse_callback_url(url)

        assert result["code"] == "abc123"
        assert result["state"] == "xyz789"
        assert result["error"] is None

    def test_parse_callback_url_invalid_url(self):
        """Test that invalid URL raises ValueError."""
        url = "not-a-valid-url"

        # Invalid URL has no query string, so state will be missing
        with pytest.raises(ValueError) as exc_info:
            parse_callback_url(url)

        assert "Missing required 'state' parameter" in str(exc_info.value)

    def test_parse_callback_url_no_query_string(self):
        """Test URL with no query string."""
        url = "http://localhost:3000/auth/callback"

        with pytest.raises(ValueError) as exc_info:
            parse_callback_url(url)

        assert "Missing required 'state' parameter" in str(exc_info.value)

    def test_parse_callback_url_multiple_values_same_param(self):
        """Test URL with multiple values for same parameter (uses first value)."""
        url = "http://localhost:3000/auth/callback?code=first&code=second&state=xyz789"

        result = parse_callback_url(url)

        assert result["code"] == "first"  # parse_qs returns first value
        assert result["state"] == "xyz789"
        assert result["error"] is None
