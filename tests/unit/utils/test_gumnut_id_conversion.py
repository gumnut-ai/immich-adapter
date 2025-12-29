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
    safe_uuid_from_face_id,
    uuid_to_gumnut_face_id,
    safe_uuid_from_user_id,
    uuid_to_gumnut_user_id,
)


class TestSafeUuidFromGumnutId:
    """Test the safe_uuid_from_gumnut_id function."""

    def test_valid_id_conversion(self):
        """Test converting valid album ID to UUID."""
        # Create a test UUID and encode it
        test_uuid = uuid4()
        short_uuid = shortuuid.encode(test_uuid)
        gumnut_id = f"prefix_{short_uuid}"

        result = safe_uuid_from_gumnut_id(gumnut_id, "prefix")

        assert result == test_uuid
        assert isinstance(result, UUID)


    def test_invalid_prefix_fallback(self):
        """Test handling of invalid prefix - should fall back to UUID parsing."""
        test_uuid = uuid4()
        gumnut_id = str(test_uuid)  # No prefix, just raw UUID

        # Since gumnut_id does not have a prefix, a ValueError should be raised
        with pytest.raises(ValueError):
            safe_uuid_from_gumnut_id(gumnut_id, "prefix_not_found")

    def test_wrong_prefix_fallback(self):
        """Test handling of wrong prefix - should fall back to UUID parsing."""
        test_uuid = uuid4()
        short_uuid = shortuuid.encode(test_uuid)
        gumnut_id = f"wrong_{short_uuid}"

        # Since gumnut_id has a different prefix than what we call safe_uuid_from_gumnut_id() with, a ValueError should be raised
        with pytest.raises(ValueError):
            safe_uuid_from_gumnut_id(gumnut_id, "different_prefix")

    def test_empty_string_handling(self):
        """Test handling of empty string."""
        with pytest.raises(ValueError):
            safe_uuid_from_gumnut_id("", "album")

    def test_known_uuid_roundtrip(self):
        """Test with a known UUID to ensure consistent behavior."""
        known_uuid = UUID("550e8400-e29b-41d4-a716-446655440000")
        short_uuid = shortuuid.encode(known_uuid)
        gumnut_id = f"prefix_{short_uuid}"

        result = safe_uuid_from_gumnut_id(gumnut_id, "prefix")

        assert result == known_uuid


class TestUuidToGumnutId:
    """Test the uuid_to_gumnut_id function."""

    def test_id_generation(self):
        """Test generating ID from UUID."""
        test_uuid = uuid4()

        result = uuid_to_gumnut_id(test_uuid, "prefix")

        assert result.startswith("prefix_")
        assert isinstance(result, str)

        # Should be reversible
        decoded = safe_uuid_from_gumnut_id(result, "prefix")
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

    def test_face_convenience_functions(self):
        """Test face-specific convenience functions."""
        test_uuid = uuid4()

        # UUID to face ID
        face_id = uuid_to_gumnut_face_id(test_uuid)
        assert face_id.startswith("face_")

        # Face ID back to UUID
        recovered_uuid = safe_uuid_from_face_id(face_id)
        assert recovered_uuid == test_uuid

    def test_user_convenience_functions(self):
        """Test user-specific convenience functions."""
        test_uuid = uuid4()

        # UUID to user ID
        user_id = uuid_to_gumnut_user_id(test_uuid)
        assert user_id.startswith("intuser_")

        # User ID back to UUID
        recovered_uuid = safe_uuid_from_user_id(user_id)
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

        # Face functions
        assert uuid_to_gumnut_face_id(test_uuid) == uuid_to_gumnut_id(
            test_uuid, "face"
        )

        face_id = f"face_{shortuuid.encode(test_uuid)}"
        assert safe_uuid_from_face_id(face_id) == safe_uuid_from_gumnut_id(
            face_id, "face"
        )

        # User functions
        assert uuid_to_gumnut_user_id(test_uuid) == uuid_to_gumnut_id(
            test_uuid, "intuser"
        )

        user_id = f"intuser_{shortuuid.encode(test_uuid)}"
        assert safe_uuid_from_user_id(user_id) == safe_uuid_from_gumnut_id(
            user_id, "intuser"
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
