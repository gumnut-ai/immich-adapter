"""Tests for album conversion utilities — focused on the Immich v3 shape.

``convert_gumnut_album_to_immich`` builds ``AlbumResponseDto``, which in Immich
v3 no longer carries ``owner``/``ownerId`` or inline ``assets``. The owner is
instead derived from ``albumUsers[0]`` (``minItems: 1``), and ``shared`` is
required. Gumnut has no album sharing, so ``albumUsers`` is always exactly the
single owner entry.
"""

import pytest

from routers.immich_models import AlbumUserRole
from routers.utils.album_conversion import convert_gumnut_album_to_immich


class TestConvertGumnutAlbumToImmich:
    def test_owner_carried_in_album_users(self, sample_gumnut_album, mock_current_user):
        """The current user is the sole albumUsers[0] entry, with the owner role."""
        result = convert_gumnut_album_to_immich(
            sample_gumnut_album, mock_current_user, asset_count=5
        )

        assert len(result.albumUsers) == 1
        assert result.albumUsers[0].role == AlbumUserRole.owner
        assert result.albumUsers[0].user.id == mock_current_user.id
        assert result.albumUsers[0].user == mock_current_user

    def test_removed_v3_fields_absent(self, sample_gumnut_album, mock_current_user):
        """owner/ownerId/assets were dropped from the v3 DTO — none are emitted."""
        result = convert_gumnut_album_to_immich(
            sample_gumnut_album, mock_current_user, asset_count=5
        )

        assert not hasattr(result, "owner")
        assert not hasattr(result, "ownerId")
        assert not hasattr(result, "assets")

    def test_shared_flags_false(self, sample_gumnut_album, mock_current_user):
        """Gumnut has no sharing, so shared/hasSharedLink are always False.

        ``shared`` is required in v3; it must be present (not omitted).
        """
        result = convert_gumnut_album_to_immich(
            sample_gumnut_album, mock_current_user, asset_count=5
        )

        assert result.shared is False
        assert result.hasSharedLink is False

    @pytest.mark.parametrize("count", [0, 5, 42])
    def test_asset_count_forwarded(self, sample_gumnut_album, mock_current_user, count):
        """The passed asset_count is echoed onto the DTO."""
        result = convert_gumnut_album_to_immich(
            sample_gumnut_album, mock_current_user, asset_count=count
        )

        assert result.assetCount == count

    def test_asset_count_defaults_to_zero(self, sample_gumnut_album, mock_current_user):
        """A missing asset_count falls back to 0 rather than erroring."""
        result = convert_gumnut_album_to_immich(sample_gumnut_album, mock_current_user)

        assert result.assetCount == 0
