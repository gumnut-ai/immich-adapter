"""Unit tests for the public config endpoints."""

import pytest

from routers.api.public import get_public_config, get_public_config_defaults
from routers.immich_models import PublicConfigDto
from routers.middleware.auth_middleware import AuthMiddleware


class TestGetPublicConfig:
    @pytest.mark.anyio
    async def test_constructs_valid_dto(self):
        config = await get_public_config()

        assert isinstance(config, PublicConfigDto)
        # These values control the login flow and its manual-launch fallback.
        assert config.oauth.enabled is True
        assert config.oauth.autoLaunch is True
        assert config.oauth.buttonText == "Sign in with Gumnut"
        assert config.passwordLogin.enabled is False

    @pytest.mark.anyio
    async def test_defaults_matches_config(self):
        """The adapter's config is fixed, so defaults mirror the live config."""
        assert await get_public_config_defaults() == await get_public_config()

    def test_routes_are_unauthenticated(self):
        """The login page fetches public config before credentials exist."""
        assert "/api/public/config" in AuthMiddleware.UNAUTHENTICATED_PATHS
        assert "/api/public/config/defaults" in AuthMiddleware.UNAUTHENTICATED_PATHS
