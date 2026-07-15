"""Unit tests for License API endpoints.

The license stubs construct a generated ``LicenseResponseDto`` (a
``RootModel`` wrapping ``UserLicense``) by hand, so a model regeneration
that tightens ``UserLicense`` or reshapes the ``RootModel`` breaks them at
construction time (pydantic ValidationError -> 500). The tests here exercise
that construction path end to end, symmetric with ``test_jobs.py`` and
``test_system_config.py``.
"""

import pytest

from routers.api.server import get_server_license
from routers.api.users import get_user_license
from routers.immich_models import LicenseResponseDto


class TestGetUserLicense:
    """Test the get_user_license endpoint."""

    @pytest.mark.anyio
    async def test_constructs_valid_dto(self):
        """The stub must construct a valid LicenseResponseDto."""
        assert isinstance(await get_user_license(), LicenseResponseDto)


class TestGetServerLicense:
    """Test the get_server_license endpoint."""

    @pytest.mark.anyio
    async def test_constructs_valid_dto(self):
        """The stub must construct a valid LicenseResponseDto."""
        assert isinstance(await get_server_license(), LicenseResponseDto)
