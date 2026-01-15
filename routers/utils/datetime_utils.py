"""
Shared datetime utilities for converting datetimes to Immich API format.

This module provides functions for converting datetime values between Gumnut
and Immich formats, handling timezone conversions and Immich's special
"keepLocalTime" format for localDateTime fields.
"""

from datetime import datetime, timezone


def to_actual_utc(dt: datetime | None) -> datetime | None:
    """
    Convert a datetime to actual UTC timestamp.

    If the datetime has timezone info, convert to UTC. If naive, assume UTC.
    This is used for fileCreatedAt and dateTimeOriginal which should be actual
    UTC so that clients can correctly convert to the user's local timezone.

    Args:
        dt: The datetime to convert, or None

    Returns:
        The datetime converted to UTC, or None if input was None
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Naive datetime - assume it's meant to be UTC
        return dt.replace(tzinfo=timezone.utc)
    # Convert timezone-aware datetime to UTC
    return dt.astimezone(timezone.utc)


def to_immich_local_datetime(dt: datetime | None) -> datetime | None:
    """
    Convert a datetime to Immich's "keepLocalTime" format.

    Immich stores localDateTime as a UTC timestamp that preserves local time values.
    For example, 10:00 AM PST becomes 10:00:00Z (not 18:00:00Z). This allows the
    mobile client to display the original local time regardless of viewer timezone.

    See immich/server/src/services/metadata.service.ts:870

    Args:
        dt: The datetime to convert, or None

    Returns:
        The datetime with local time values preserved as UTC, or None if input was None
    """
    if dt is None:
        return None
    # Strip timezone info, then mark as UTC to preserve the local time appearance
    return dt.replace(tzinfo=None).replace(tzinfo=timezone.utc)


def format_timezone_immich(dt: datetime | None) -> str | None:
    """
    Format timezone in Immich's format (e.g., 'UTC+9', 'UTC-8', 'UTC+5:30').

    Immich stores timezone from exiftool which uses 'UTC+X' format without
    leading zeros. We need to match this format for consistency.

    Args:
        dt: A timezone-aware datetime, or None

    Returns:
        Timezone string in Immich format (e.g., 'UTC-8', 'UTC+5:30'),
        or None if no timezone info available
    """
    if dt is None or dt.tzinfo is None:
        return None

    # Get the UTC offset as a timedelta
    offset = dt.utcoffset()
    if offset is None:
        return None

    # Calculate total seconds and convert to hours/minutes
    total_seconds = int(offset.total_seconds())
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes = remainder // 60

    # Build the timezone string in Immich's format
    sign = "+" if total_seconds >= 0 else "-"
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    else:
        return f"UTC{sign}{hours}"
