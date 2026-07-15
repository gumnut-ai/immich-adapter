"""Unit tests for System Config API endpoints.

These stubs build large trees of generated DTOs by hand, so a model
regeneration that adds required fields or retypes existing ones breaks them
at construction time (pydantic ValidationError -> 500). The tests here
exercise that construction path end to end.
"""

import pytest

from routers.api.system_config import (
    get_config,
    get_config_defaults,
    get_storage_template_options,
)
from routers.immich_models import (
    SystemConfigDto,
    SystemConfigTemplateStorageOptionDto,
)


class TestGetConfig:
    """Test the get_config endpoint."""

    @pytest.mark.anyio
    async def test_get_config_constructs_valid_dto(self):
        """The stub config must validate against the generated models."""
        config = await get_config()

        assert isinstance(config, SystemConfigDto)
        # OAuth is the only login method the adapter supports; these two
        # values are load-bearing for the Immich clients' login flow.
        assert config.oauth.enabled is True
        assert config.passwordLogin.enabled is False
        # Real-time HLS is an intentional gap; disabling it keeps both clients
        # on direct playback.
        assert config.ffmpeg.realtime.enabled is False

    @pytest.mark.anyio
    async def test_get_config_defaults_matches_config(self):
        """The defaults stub returns the same configuration as get_config."""
        assert await get_config_defaults() == await get_config()


class TestGetStorageTemplateOptions:
    """Test the get_storage_template_options endpoint."""

    @pytest.mark.anyio
    async def test_constructs_valid_dto(self):
        options = await get_storage_template_options()

        assert isinstance(options, SystemConfigTemplateStorageOptionDto)
