"""Unit tests for the public config endpoints (Immich v3.2+).

The v3.2 web login page calls ``getPublicConfig`` before any authentication,
so these tests pin both the hand-built DTO construction (a model regen that
adds required fields breaks it at runtime, not in pyright) and the
load-bearing values the login flow reads.
"""

import pytest

from routers.api.public import get_public_config, get_public_config_defaults
from routers.immich_models import PublicConfigDto
from routers.middleware.auth_middleware import AuthMiddleware


class TestGetPublicConfig:
    @pytest.mark.anyio
    async def test_constructs_valid_dto(self):
        config = await get_public_config()

        assert isinstance(config, PublicConfigDto)
        # OAuth is the only login method; these values drive the web login
        # page (auto-launch into the OAuth flow, button label as fallback).
        assert config.oauth.enabled is True
        assert config.oauth.autoLaunch is True
        assert config.oauth.buttonText == "Sign in with Gumnut"
        assert config.passwordLogin.enabled is False

    @pytest.mark.anyio
    async def test_defaults_matches_config(self):
        """The adapter's config is fixed, so defaults mirror the live config."""
        assert await get_public_config_defaults() == await get_public_config()

    def test_routes_are_unauthenticated(self):
        """The login page fetches public config before any credential exists;
        a missing UNAUTHENTICATED_PATHS entry turns login into a 401."""
        assert "/api/public/config" in AuthMiddleware.UNAUTHENTICATED_PATHS
        assert "/api/public/config/defaults" in AuthMiddleware.UNAUTHENTICATED_PATHS
