"""
Tests for gumnut_id_conversion utilities.
"""

import pytest
from uuid import UUID, uuid4
import shortuuid

from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_gumnut_id,
    uuid_to_gumnut_id,
    safe_uuid_from_album_id,
    uuid_to_gumnut_album_id,
    safe_uuid_from_asset_id,
    uuid_to_gumnut_asset_id,
    safe_uuid_from_person_id,
    uuid_to_gumnut_person_id,
)


class TestSafeUuidFromGumnutId:
    """Test the safe_uuid_from_gumnut_id function."""

    def test_valid_album_id_conversion(self):
        """Test converting valid album ID to UUID."""
        # Create a test UUID and encode it
        test_uuid = uuid4()
        short_uuid = shortuuid.encode(test_uuid)
        gumnut_id = f"album_{short_uuid}"

        result = safe_uuid_from_gumnut_id(gumnut_id, "album")

        assert result == test_uuid
        assert isinstance(result, UUID)

    def test_valid_asset_id_conversion(self):
        """Test converting valid asset ID to UUID."""
        test_uuid = uuid4()
        short_uuid = shortuuid.encode(test_uuid)
        gumnut_id = f"asset_{short_uuid}"

        result = safe_uuid_from_gumnut_id(gumnut_id, "asset")

        assert result == test_uuid
        assert isinstance(result, UUID)

    def test_valid_person_id_conversion(self):
        """Test converting valid person ID to UUID."""
        test_uuid = uuid4()
        short_uuid = shortuuid.encode(test_uuid)
        gumnut_id = f"person_{short_uuid}"

        result = safe_uuid_from_gumnut_id(gumnut_id, "person")

        assert result == test_uuid
        assert isinstance(result, UUID)

    def test_invalid_prefix_fallback(self):
        """Test handling of invalid prefix - should fall back to UUID parsing."""
        test_uuid = uuid4()
        gumnut_id = str(test_uuid)  # No prefix, just raw UUID

        # Since gumnut_id does not have a prefix, a ValueError should be raised
        with pytest.raises(ValueError):
            safe_uuid_from_gumnut_id(gumnut_id, "album")

    def test_wrong_prefix_fallback(self):
        """Test handling of wrong prefix - should fall back to UUID parsing."""
        test_uuid = uuid4()
        short_uuid = shortuuid.encode(test_uuid)
        gumnut_id = f"wrong_{short_uuid}"

        # Since gumnut_id has a different prefix than what we call safe_uuid_from_gumnut_id() with, a ValueError should be raised
        with pytest.raises(ValueError):
            safe_uuid_from_gumnut_id(gumnut_id, "album")

    def test_empty_string_handling(self):
        """Test handling of empty string."""
        with pytest.raises(ValueError):
            safe_uuid_from_gumnut_id("", "album")

    def test_known_uuid_roundtrip(self):
        """Test with a known UUID to ensure consistent behavior."""
        known_uuid = UUID("550e8400-e29b-41d4-a716-446655440000")
        short_uuid = shortuuid.encode(known_uuid)
        gumnut_id = f"album_{short_uuid}"

        result = safe_uuid_from_gumnut_id(gumnut_id, "album")

        assert result == known_uuid


class TestUuidToGumnutId:
    """Test the uuid_to_gumnut_id function."""

    def test_album_id_generation(self):
        """Test generating album ID from UUID."""
        test_uuid = uuid4()

        result = uuid_to_gumnut_id(test_uuid, "album")

        assert result.startswith("album_")
        assert isinstance(result, str)

        # Should be reversible
        decoded = safe_uuid_from_gumnut_id(result, "album")
        assert decoded == test_uuid

    def test_asset_id_generation(self):
        """Test generating asset ID from UUID."""
        test_uuid = uuid4()

        result = uuid_to_gumnut_id(test_uuid, "asset")

        assert result.startswith("asset_")
        assert isinstance(result, str)

        # Should be reversible
        decoded = safe_uuid_from_gumnut_id(result, "asset")
        assert decoded == test_uuid

    def test_person_id_generation(self):
        """Test generating person ID from UUID."""
        test_uuid = uuid4()

        result = uuid_to_gumnut_id(test_uuid, "person")

        assert result.startswith("person_")
        assert isinstance(result, str)

        # Should be reversible
        decoded = safe_uuid_from_gumnut_id(result, "person")
        assert decoded == test_uuid

    def test_known_uuid_conversion(self):
        """Test with a known UUID for consistent results."""
        known_uuid = UUID("550e8400-e29b-41d4-a716-446655440000")

        result = uuid_to_gumnut_id(known_uuid, "test")

        assert result.startswith("test_")
        # The shortuuid encoding should be deterministic for the same UUID
        expected_short = shortuuid.encode(known_uuid)
        assert result == f"test_{expected_short}"


class TestConvenienceFunctions:
    """Test the convenience functions for specific types."""

    def test_album_convenience_functions(self):
        """Test album-specific convenience functions."""
        test_uuid = uuid4()

        # UUID to album ID
        album_id = uuid_to_gumnut_album_id(test_uuid)
        assert album_id.startswith("album_")

        # Album ID back to UUID
        recovered_uuid = safe_uuid_from_album_id(album_id)
        assert recovered_uuid == test_uuid

    def test_asset_convenience_functions(self):
        """Test asset-specific convenience functions."""
        test_uuid = uuid4()

        # UUID to asset ID
        asset_id = uuid_to_gumnut_asset_id(test_uuid)
        assert asset_id.startswith("asset_")

        # Asset ID back to UUID
        recovered_uuid = safe_uuid_from_asset_id(asset_id)
        assert recovered_uuid == test_uuid

    def test_person_convenience_functions(self):
        """Test person-specific convenience functions."""
        test_uuid = uuid4()

        # UUID to person ID
        person_id = uuid_to_gumnut_person_id(test_uuid)
        assert person_id.startswith("person_")

        # Person ID back to UUID
        recovered_uuid = safe_uuid_from_person_id(person_id)
        assert recovered_uuid == test_uuid

    def test_convenience_functions_equivalence(self):
        """Test that convenience functions are equivalent to generic functions."""
        test_uuid = uuid4()

        # Album functions
        assert uuid_to_gumnut_album_id(test_uuid) == uuid_to_gumnut_id(
            test_uuid, "album"
        )

        album_id = f"album_{shortuuid.encode(test_uuid)}"
        assert safe_uuid_from_album_id(album_id) == safe_uuid_from_gumnut_id(
            album_id, "album"
        )

        # Asset functions
        assert uuid_to_gumnut_asset_id(test_uuid) == uuid_to_gumnut_id(
            test_uuid, "asset"
        )

        asset_id = f"asset_{shortuuid.encode(test_uuid)}"
        assert safe_uuid_from_asset_id(asset_id) == safe_uuid_from_gumnut_id(
            asset_id, "asset"
        )

        # Person functions
        assert uuid_to_gumnut_person_id(test_uuid) == uuid_to_gumnut_id(
            test_uuid, "person"
        )

        person_id = f"person_{shortuuid.encode(test_uuid)}"
        assert safe_uuid_from_person_id(person_id) == safe_uuid_from_gumnut_id(
            person_id, "person"
        )


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_nil_uuid_handling(self):
        """Test handling of nil UUID (all zeros)."""
        nil_uuid = UUID("00000000-0000-0000-0000-000000000000")

        # Should work with nil UUID
        gumnut_id = uuid_to_gumnut_id(nil_uuid, "test")
        recovered = safe_uuid_from_gumnut_id(gumnut_id, "test")

        assert recovered == nil_uuid

    def test_max_uuid_handling(self):
        """Test handling of max UUID (all f's)."""
        max_uuid = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

        # Should work with max UUID
        gumnut_id = uuid_to_gumnut_id(max_uuid, "test")
        recovered = safe_uuid_from_gumnut_id(gumnut_id, "test")

        assert recovered == max_uuid

    def test_prefix_variations(self):
        """Test different prefix variations."""
        test_uuid = uuid4()

        prefixes = ["", "a", "long_prefix_name", "123", "prefix-with-dash"]

        for prefix in prefixes:
            gumnut_id = uuid_to_gumnut_id(test_uuid, prefix)
            recovered = safe_uuid_from_gumnut_id(gumnut_id, prefix)
            assert recovered == test_uuid, f"Failed with prefix: {prefix}"
