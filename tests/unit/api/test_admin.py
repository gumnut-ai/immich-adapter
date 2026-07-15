"""Unit tests for Admin API endpoints.

Unlike the module-level preferences stub in `routers/api/users.py`, whose
breakage surfaces at import, these stubs build the DTO inline and fail only
when a client hits the route — so they need explicit construction coverage.
See code-practices § "Bumping the Immich Version".
"""

from uuid import uuid4

import pytest

from routers.api.admin import (
    get_user_preferences_admin,
    update_user_preferences_admin,
)
from routers.immich_models import (
    UserPreferencesResponseDto,
    UserPreferencesUpdateDto,
)


class TestUserPreferencesAdmin:
    """Test the admin user-preferences stub endpoints."""

    @pytest.mark.anyio
    async def test_get_constructs_valid_dto(self):
        """The stub preferences must validate against the generated models."""
        result = await get_user_preferences_admin(uuid4())

        assert isinstance(result, UserPreferencesResponseDto)

    @pytest.mark.anyio
    async def test_update_constructs_valid_dto(self):
        """The update stub builds the same tree and must validate too."""
        result = await update_user_preferences_admin(
            uuid4(), UserPreferencesUpdateDto()
        )

        assert isinstance(result, UserPreferencesResponseDto)

    @pytest.mark.anyio
    async def test_recently_added_present_and_hidden(self):
        """`recentlyAdded` (Immich v3.0.1) is emitted, sidebar link hidden."""
        result = await get_user_preferences_admin(uuid4())

        assert result.recentlyAdded.sidebarWeb is False
