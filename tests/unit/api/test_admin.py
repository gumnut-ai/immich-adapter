"""Unit tests for Admin API endpoints.

Both preference stubs return one module-level `UserPreferencesResponseDto`, so
a regen that adds a required field breaks them at import and these tests fail
at collection. What import alone can't catch is the values the client gates on
and the update endpoint's ignore-the-request contract; the tests below pin
those, alongside a construction check mirroring `test_users.py`.
See code-practices § "Bumping the Immich Version".
"""

from uuid import uuid4

import pytest

from routers.api.admin import (
    get_user_preferences_admin,
    update_user_preferences_admin,
)
from routers.immich_models import (
    RecentlyAddedUpdate,
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
    async def test_update_ignores_the_request(self):
        """The update stub discards the request rather than applying it.

        The update must be non-empty to distinguish "ignored" from "applied" —
        an all-`None` payload echoes unchanged preferences either way.
        """
        result = await update_user_preferences_admin(
            uuid4(),
            UserPreferencesUpdateDto(
                recentlyAdded=RecentlyAddedUpdate(sidebarWeb=True)
            ),
        )

        assert isinstance(result, UserPreferencesResponseDto)
        assert result.recentlyAdded.sidebarWeb is False

    @pytest.mark.anyio
    async def test_recently_added_present_and_hidden(self):
        """`recentlyAdded` (Immich v3.0.1) is emitted, sidebar link hidden."""
        result = await get_user_preferences_admin(uuid4())

        assert result.recentlyAdded.sidebarWeb is False
