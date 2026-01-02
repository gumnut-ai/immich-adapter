"""Tests for sync.py ack parsing and generation helpers."""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from routers.api.sync import _parse_ack, _to_ack_string
from routers.immich_models import SyncEntityType


class TestParseAck:
    """Tests for _parse_ack helper function.

    Ack format for immich-adapter: "SyncEntityType|timestamp|"
    """

    def test_parse_valid_ack(self):
        """Parse a valid ack string with entity type and timestamp."""
        ack = "AssetV1|2025-01-20T10:30:45.123456+00:00|"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, timestamp = result

        assert entity_type == SyncEntityType.AssetV1
        assert timestamp is not None
        assert timestamp.year == 2025
        assert timestamp.month == 1
        assert timestamp.day == 20
        assert timestamp.hour == 10
        assert timestamp.minute == 30

    def test_parse_ack_with_timezone(self):
        """Parse ack with non-UTC timezone offset."""
        ack = "AssetV1|2025-01-20T10:30:45+09:00|"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, timestamp = result

        assert entity_type == SyncEntityType.AssetV1
        assert timestamp is not None
        # Timestamp should preserve the timezone info
        assert timestamp.hour == 10

    def test_parse_ack_without_timestamp(self):
        """Parse ack with empty timestamp (should return None for timestamp)."""
        ack = "AssetV1||"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, timestamp = result

        assert entity_type == SyncEntityType.AssetV1
        assert timestamp is None

    def test_parse_ack_invalid_format_too_few_parts(self):
        """Malformed ack with too few parts returns None (skipped)."""
        ack = "AssetV1"
        result = _parse_ack(ack)
        assert result is None

    def test_parse_ack_invalid_entity_type(self):
        """Invalid entity type throws HTTPException (matches immich behavior)."""
        ack = "InvalidType|2025-01-20T10:30:45+00:00|"
        with pytest.raises(HTTPException) as exc_info:
            _parse_ack(ack)

        assert exc_info.value.status_code == 400
        assert "Invalid ack type" in str(exc_info.value.detail)

    def test_parse_ack_invalid_timestamp(self):
        """Invalid timestamp returns None (skipped, not thrown)."""
        ack = "AssetV1|not-a-timestamp|"
        result = _parse_ack(ack)
        assert result is None


class TestToAckString:
    """Tests for _to_ack_string helper function.

    Generates ack strings in format: "SyncEntityType|timestamp|"
    """

    def test_ack_string_format_is_pipe_delimited(self):
        """Verify ack string uses pipe delimiters."""
        timestamp = datetime(2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc)
        result = _to_ack_string(SyncEntityType.AssetV1, timestamp)

        parts = result.split("|")
        assert len(parts) == 3  # Type, timestamp, trailing empty
        assert parts[0] == "AssetV1"
        assert parts[2] == ""  # Trailing pipe for future additions

    def test_roundtrip_parse_and_generate(self):
        """Verify ack string can be parsed back to original values."""
        original_type = SyncEntityType.AssetExifV1
        original_timestamp = datetime(2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc)

        ack_string = _to_ack_string(original_type, original_timestamp)
        result = _parse_ack(ack_string)
        assert result is not None
        parsed_type, parsed_timestamp = result

        assert parsed_type == original_type
        assert parsed_timestamp is not None
        # Compare as ISO strings since microseconds might differ
        assert parsed_timestamp.isoformat() == original_timestamp.isoformat()
