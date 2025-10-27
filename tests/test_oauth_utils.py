import pytest
from routers.utils.oauth_utils import parse_callback_url


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
