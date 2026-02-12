"""Tests for sync.py ack parsing and generation helpers."""

import pytest
from fastapi import HTTPException

from routers.api.sync.routes import _parse_ack
from routers.api.sync.stream import _to_ack_string
from routers.immich_models import SyncEntityType


class TestParseAck:
    """Tests for _parse_ack helper function.

    Ack format for immich-adapter: "SyncEntityType|cursor|"
    """

    def test_parse_valid_ack(self):
        """Parse a valid ack string with entity type and cursor."""
        ack = "AssetV1|event_cursor_abc123|"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, cursor = result

        assert entity_type == SyncEntityType.AssetV1
        assert cursor == "event_cursor_abc123"

    def test_parse_ack_with_empty_cursor(self):
        """Parse ack with empty cursor returns empty string."""
        ack = "AssetV1||"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, cursor = result

        assert entity_type == SyncEntityType.AssetV1
        assert cursor == ""

    def test_parse_ack_minimal_format(self):
        """Parse ack with just entity type and cursor (no trailing pipe)."""
        ack = "AssetV1|event_cursor_abc"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, cursor = result

        assert entity_type == SyncEntityType.AssetV1
        assert cursor == "event_cursor_abc"

    def test_parse_ack_invalid_format_too_few_parts(self):
        """Malformed ack with too few parts returns None (skipped)."""
        ack = "AssetV1"
        result = _parse_ack(ack)
        assert result is None

    def test_parse_ack_invalid_entity_type(self):
        """Invalid entity type throws HTTPException (matches immich behavior)."""
        ack = "InvalidType|event_cursor_abc|"
        with pytest.raises(HTTPException) as exc_info:
            _parse_ack(ack)

        assert exc_info.value.status_code == 400
        assert "Invalid ack type" in str(exc_info.value.detail)

    def test_parse_ack_extra_pipes_accepted(self):
        """Ack with extra pipe-delimited fields is still parsed (forward-compatible)."""
        ack = "AssetV1|event_cursor_abc|extra_field|"
        result = _parse_ack(ack)
        assert result is not None
        entity_type, cursor = result

        assert entity_type == SyncEntityType.AssetV1
        assert cursor == "event_cursor_abc"


class TestToAckString:
    """Tests for _to_ack_string helper function.

    Generates ack strings in format: "SyncEntityType|cursor|"
    """

    def test_ack_string_format_is_pipe_delimited(self):
        """Verify ack string uses pipe delimiters."""
        result = _to_ack_string(SyncEntityType.AssetV1, "event_cursor_abc")

        parts = result.split("|")
        assert len(parts) == 3  # Type, cursor, trailing empty
        assert parts[0] == "AssetV1"
        assert parts[1] == "event_cursor_abc"
        assert parts[2] == ""  # Trailing pipe for future additions

    def test_ack_string_with_empty_cursor(self):
        """Verify ack string with empty cursor."""
        result = _to_ack_string(SyncEntityType.AssetV1, "")

        parts = result.split("|")
        assert len(parts) == 3
        assert parts[1] == ""

    def test_roundtrip_parse_and_generate(self):
        """Verify ack string can be parsed back to original values."""
        original_type = SyncEntityType.AssetExifV1
        original_cursor = "event_cursor_exif789"

        ack_string = _to_ack_string(original_type, original_cursor)
        result = _parse_ack(ack_string)
        assert result is not None
        parsed_type, parsed_cursor = result

        assert parsed_type == original_type
        assert parsed_cursor == original_cursor

    def test_roundtrip_with_empty_cursor(self):
        """Verify roundtrip with empty cursor."""
        original_type = SyncEntityType.AlbumV1

        ack_string = _to_ack_string(original_type, "")
        result = _parse_ack(ack_string)
        assert result is not None
        parsed_type, parsed_cursor = result

        assert parsed_type == original_type
        assert parsed_cursor == ""
