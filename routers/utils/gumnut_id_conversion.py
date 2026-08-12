"""
Utility functions for converting between Gumnut IDs and UUIDs.

Gumnut uses short UUIDs with prefixes like 'album_' and 'asset_'
while Immich expects regular UUIDs.
"""

import logging
from uuid import UUID

import shortuuid

logger = logging.getLogger(__name__)


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


def safe_uuid_from_stack_id(stack_id: str) -> UUID:
    """Convert a burst-stack ID to UUID.

    The prefix is the compound ``asset_stack`` (yielding ``asset_stack_``), not
    ``asset``. Because an asset ID's ``asset_`` prefix is a strict prefix of it,
    the two are mutually exclusive rather than merely different: feeding a stack
    ID to ``safe_uuid_from_asset_id`` leaves ``stack_<short>``, whose underscore
    is outside shortuuid's alphabet, so it raises rather than silently decoding
    to some other entity's UUID.
    """
    return safe_uuid_from_gumnut_id(stack_id, "asset_stack")


def uuid_to_gumnut_stack_id(uuid_obj: UUID) -> str:
    """Convert UUID to burst-stack ID."""
    return uuid_to_gumnut_id(uuid_obj, "asset_stack")


def immich_stack_id(gumnut_stack_id: str | None) -> str | None:
    """Map a Gumnut stack FK to the Immich ``stackId`` string, or ``None``.

    Returns ``None`` for a loose asset (no stack) and also degrades an
    undecodable stack ID to ``None`` rather than raising — a backend prefix
    change would otherwise break every stacked asset's conversion. Shared by the
    sync asset converter and the upload-ready WebSocket payload so both treat an
    invalid ID identically (mirroring ``resolve_timeline_stacks``'s fallback).
    """
    if not gumnut_stack_id:
        return None
    try:
        return str(safe_uuid_from_stack_id(gumnut_stack_id))
    except ValueError:
        # Prefix drift affects every stack, so log once at debug, not per asset.
        logger.debug(
            "Asset stack_id is not decodable to an Immich UUID; "
            "treating the asset as loose (stackId=None)",
            extra={"stack_id": gumnut_stack_id},
        )
        return None


def safe_uuid_from_person_id(person_id: str) -> UUID:
    """Convert person ID to UUID."""
    return safe_uuid_from_gumnut_id(person_id, "person")


def uuid_to_gumnut_person_id(uuid_obj: UUID) -> str:
    """Convert UUID to person ID."""
    return uuid_to_gumnut_id(uuid_obj, "person")


def safe_uuid_from_face_id(face_id: str) -> UUID:
    """Convert face ID to UUID."""
    return safe_uuid_from_gumnut_id(face_id, "face")


def uuid_to_gumnut_face_id(uuid_obj: UUID) -> str:
    """Convert UUID to face ID."""
    return uuid_to_gumnut_id(uuid_obj, "face")


def safe_uuid_from_user_id(user_id: str) -> UUID:
    """Convert user ID to UUID."""
    return safe_uuid_from_gumnut_id(user_id, "intuser")


def uuid_to_gumnut_user_id(uuid_obj: UUID) -> str:
    """Convert UUID to user ID."""
    return uuid_to_gumnut_id(uuid_obj, "intuser")
