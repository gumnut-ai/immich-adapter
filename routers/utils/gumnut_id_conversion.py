"""
Utility functions for converting between Gumnut IDs and UUIDs.

Gumnut uses short UUIDs with prefixes like 'album_' and 'asset_'
while Immich expects regular UUIDs.
"""

from uuid import UUID

import shortuuid


def safe_uuid_from_gumnut_id(gumnut_id: str, prefix: str) -> UUID:
    """
    Convert Gumnut ID to a valid UUID.
    Gumnut IDs have format: {prefix}_{short_uuid}
    The short_uuid is a shortuuid-encoded UUID that needs to be decoded.

    Args:
        gumnut_id: The Gumnut ID (e.g., 'album_BM3nUmJ6fkBqBADyz5FEiu')
        prefix: Expected prefix (e.g., 'album', 'asset').

    Returns:
        UUID object

    Throws:
        ValueError if the gumnut_id is not in the expected format or cannot be decoded
    """
    expected_prefix = f"{prefix}_"
    if gumnut_id.startswith(expected_prefix):
        short_uuid_part = gumnut_id[len(expected_prefix) :]
        # Decode the short UUID back to a regular UUID
        return shortuuid.decode(short_uuid_part)
    else:
        # should not reasonably happen
        raise ValueError(
            f"Invalid Gumnut ID format: {gumnut_id}, expected prefix: {expected_prefix}"
        )


def uuid_to_gumnut_id(uuid_obj: UUID, prefix: str) -> str:
    """
    Convert a UUID back to Gumnut ID format.
    This reverses the process of safe_uuid_from_gumnut_id.

    Args:
        uuid_obj: The UUID to convert
        prefix: The prefix to add (e.g., 'album', 'asset')

    Returns:
        Gumnut ID string (e.g., 'album_BM3nUmJ6fkBqBADyz5FEiu')
    """
    # Encode the UUID as a short UUID and add the prefix
    short_uuid = shortuuid.encode(uuid_obj)
    return f"{prefix}_{short_uuid}"


# Convenience functions for specific types
def safe_uuid_from_album_id(album_id: str) -> UUID:
    """Convert album ID to UUID."""
    return safe_uuid_from_gumnut_id(album_id, "album")


def uuid_to_gumnut_album_id(uuid_obj: UUID) -> str:
    """Convert UUID to album ID."""
    return uuid_to_gumnut_id(uuid_obj, "album")


def safe_uuid_from_asset_id(asset_id: str) -> UUID:
    """Convert asset ID to UUID."""
    return safe_uuid_from_gumnut_id(asset_id, "asset")


def uuid_to_gumnut_asset_id(uuid_obj: UUID) -> str:
    """Convert UUID to asset ID."""
    return uuid_to_gumnut_id(uuid_obj, "asset")


def safe_uuid_from_person_id(person_id: str) -> UUID:
    """Convert person ID to UUID."""
    return safe_uuid_from_gumnut_id(person_id, "person")


def uuid_to_gumnut_person_id(uuid_obj: UUID) -> str:
    """Convert UUID to person ID."""
    return uuid_to_gumnut_id(uuid_obj, "person")
