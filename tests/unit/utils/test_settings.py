from config.settings import Settings


class TestSettingsOAuthRedirectUris:
    """Test OAuth redirect URI settings parsing."""

    def test_default_redirect_uri(self):
        """Test default value when env var not set."""
        settings = Settings(environment="test")
        assert settings.oauth_allowed_redirect_uris_list == {
            "http://localhost:3000/auth/callback"
        }

    def test_single_redirect_uri(self):
        """Test parsing single URI."""
        settings = Settings(
            environment="test",
            oauth_allowed_redirect_uris="https://app.example.com/callback",
        )
        assert len(settings.oauth_allowed_redirect_uris_list) == 1
        assert (
            "https://app.example.com/callback"
            in settings.oauth_allowed_redirect_uris_list
        )

    def test_multiple_redirect_uris(self):
        """Test parsing comma-separated URIs."""
        settings = Settings(
            environment="test",
            oauth_allowed_redirect_uris="http://localhost:3000/callback,https://app.example.com/callback,https://staging.example.com/callback",
        )
        uris = settings.oauth_allowed_redirect_uris_list
        assert len(uris) == 3
        assert "http://localhost:3000/callback" in uris
        assert "https://app.example.com/callback" in uris
        assert "https://staging.example.com/callback" in uris
