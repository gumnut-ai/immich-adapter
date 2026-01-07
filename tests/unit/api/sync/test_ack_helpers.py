"""Tests for sync.py ack parsing and generation helpers."""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from routers.api.sync import _parse_ack, _to_ack_string
from routers.immich_models import SyncEntityType


class TestParseAck:
    """Tests for _parse_ack helper function.

    Ack format for immich-adapter: "SyncEntityType|timestamp|entity_id|"
    """

    def test_parse_valid_ack(self):
        """Parse a valid ack string with entity type, timestamp, and entity_id."""
        ack = "AssetV1|2025-01-20T10:30:45.123456+00:00|asset-123|"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, timestamp, entity_id = result

        assert entity_type == SyncEntityType.AssetV1
        assert timestamp.year == 2025
        assert timestamp.month == 1
        assert timestamp.day == 20
        assert timestamp.hour == 10
        assert timestamp.minute == 30
        assert entity_id == "asset-123"

    def test_parse_ack_with_timezone(self):
        """Parse ack with non-UTC timezone offset."""
        ack = "AssetV1|2025-01-20T10:30:45+09:00|asset-456|"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, timestamp, entity_id = result

        assert entity_type == SyncEntityType.AssetV1
        # Timestamp should preserve the timezone info
        assert timestamp.hour == 10
        assert entity_id == "asset-456"

    def test_parse_ack_without_timestamp_is_skipped(self):
        """Parse ack with empty timestamp returns None (skipped)."""
        ack = "AssetV1||entity-123|"
        result = _parse_ack(ack)
        # Missing timestamp should cause the ack to be skipped
        assert result is None

    def test_parse_ack_without_entity_id(self):
        """Parse ack with empty entity_id returns empty string."""
        ack = "AssetV1|2025-01-20T10:30:45+00:00||"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, timestamp, entity_id = result

        assert entity_type == SyncEntityType.AssetV1
        assert timestamp is not None
        assert entity_id == ""

    def test_parse_ack_old_format_backward_compatible(self):
        """Parse old format ack without entity_id field (backward compatible)."""
        ack = "AssetV1|2025-01-20T10:30:45+00:00|"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, timestamp, entity_id = result

        assert entity_type == SyncEntityType.AssetV1
        assert timestamp is not None
        assert entity_id == ""  # Empty string for missing entity_id

    def test_parse_ack_invalid_format_too_few_parts(self):
        """Malformed ack with too few parts returns None (skipped)."""
        ack = "AssetV1"
        result = _parse_ack(ack)
        assert result is None

    def test_parse_ack_invalid_entity_type(self):
        """Invalid entity type throws HTTPException (matches immich behavior)."""
        ack = "InvalidType|2025-01-20T10:30:45+00:00|entity-123|"
        with pytest.raises(HTTPException) as exc_info:
            _parse_ack(ack)

        assert exc_info.value.status_code == 400
        assert "Invalid ack type" in str(exc_info.value.detail)

    def test_parse_ack_invalid_timestamp(self):
        """Invalid timestamp returns None (skipped, not thrown)."""
        ack = "AssetV1|not-a-timestamp|entity-123|"
        result = _parse_ack(ack)
        assert result is None


class TestToAckString:
    """Tests for _to_ack_string helper function.

    Generates ack strings in format: "SyncEntityType|timestamp|entity_id|"
    """

    def test_ack_string_format_is_pipe_delimited(self):
        """Verify ack string uses pipe delimiters."""
        timestamp = datetime(2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc)
        result = _to_ack_string(SyncEntityType.AssetV1, timestamp, "asset-123")

        parts = result.split("|")
        assert len(parts) == 4  # Type, timestamp, entity_id, trailing empty
        assert parts[0] == "AssetV1"
        assert parts[2] == "asset-123"
        assert parts[3] == ""  # Trailing pipe for future additions

    def test_ack_string_with_empty_entity_id(self):
        """Verify ack string with empty entity_id."""
        timestamp = datetime(2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc)
        result = _to_ack_string(SyncEntityType.AssetV1, timestamp, "")

        parts = result.split("|")
        assert len(parts) == 4
        assert parts[2] == ""

    def test_roundtrip_parse_and_generate(self):
        """Verify ack string can be parsed back to original values."""
        original_type = SyncEntityType.AssetExifV1
        original_timestamp = datetime(2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc)
        original_entity_id = "exif-asset-789"

        ack_string = _to_ack_string(
            original_type, original_timestamp, original_entity_id
        )
        result = _parse_ack(ack_string)
        assert result is not None
        parsed_type, parsed_timestamp, parsed_entity_id = result

        assert parsed_type == original_type
        assert parsed_timestamp is not None
        # Compare as ISO strings since microseconds might differ
        assert parsed_timestamp.isoformat() == original_timestamp.isoformat()
        assert parsed_entity_id == original_entity_id

    def test_roundtrip_with_empty_entity_id(self):
        """Verify roundtrip with empty entity_id."""
        original_type = SyncEntityType.AlbumV1
        original_timestamp = datetime(2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc)

        ack_string = _to_ack_string(original_type, original_timestamp, "")
        result = _parse_ack(ack_string)
        assert result is not None
        parsed_type, parsed_timestamp, parsed_entity_id = result

        assert parsed_type == original_type
        assert parsed_entity_id == ""
