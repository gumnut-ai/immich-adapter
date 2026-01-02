"""Tests for sync.py date conversion functions, ack helpers, and stream generation."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi import HTTPException
from gumnut.types.album_asset_event_payload import AlbumAssetEventPayload
from gumnut.types.album_event_payload import AlbumEventPayload
from gumnut.types.asset_event_payload import AssetEventPayload
from gumnut.types.exif_event_payload import ExifEventPayload
from gumnut.types.face_event_payload import FaceEventPayload
from gumnut.types.person_event_payload import PersonEventPayload

from routers.api.sync import (
    _extract_timezone,
    _get_session_token,
    _parse_ack,
    _to_ack_string,
    _to_actual_utc,
    _to_immich_local_datetime,
    delete_sync_ack,
    generate_sync_stream,
    get_sync_ack,
    get_sync_stream,
    gumnut_asset_to_sync_asset_v1,
    send_sync_ack,
)
from services.checkpoint_store import Checkpoint, CheckpointStore
from services.session_store import SessionStore
from routers.immich_models import (
    SyncAckDeleteDto,
    SyncAckSetDto,
    SyncEntityType,
    SyncRequestType,
    SyncStreamDto,
)
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_face_id,
    uuid_to_gumnut_person_id,
    uuid_to_gumnut_user_id,
)

TEST_UUID = UUID("12345678-1234-1234-1234-123456789abc")
TEST_SESSION_UUID = UUID("550e8400-e29b-41d4-a716-446655440000")


class TestGetSessionToken:
    """Tests for _get_session_token helper function."""

    def test_valid_uuid_string_returns_uuid(self):
        """Valid UUID string in request.state returns UUID object."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        result = _get_session_token(mock_request)

        assert result == TEST_SESSION_UUID
        assert isinstance(result, UUID)

    def test_missing_session_token_raises_403(self):
        """Missing or None session_token raises 403."""
        mock_request = Mock()
        mock_request.state = Mock(spec=[])  # No session_token attribute

        with pytest.raises(HTTPException) as exc_info:
            _get_session_token(mock_request)

        assert exc_info.value.status_code == 403
        assert "Invalid session token" in exc_info.value.detail

    def test_invalid_uuid_string_raises_403(self):
        """Invalid UUID string raises 403."""
        mock_request = Mock()
        mock_request.state.session_token = "not-a-valid-uuid"

        with pytest.raises(HTTPException) as exc_info:
            _get_session_token(mock_request)

        assert exc_info.value.status_code == 403
        assert "Invalid session token" in exc_info.value.detail


class TestToImmichLocalDatetime:
    """Tests for _to_immich_local_datetime helper function.

    This function converts datetimes to Immich's "keepLocalTime" format,
    where local time values are stored as if they were UTC.
    """

    def test_none_input_returns_none(self):
        """None input should return None."""
        assert _to_immich_local_datetime(None) is None

    def test_utc_datetime_preserves_time_values(self):
        """UTC datetime should preserve the time values as UTC."""
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
        result = _to_immich_local_datetime(dt)

        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 10
        assert result.minute == 30
        assert result.second == 45
        assert result.tzinfo == timezone.utc

    def test_timezone_aware_datetime_preserves_local_time_as_utc(self):
        """Timezone-aware datetime should strip timezone and mark as UTC.

        This is the "keepLocalTime" format Immich uses: local time values
        stored as if they were UTC.
        """
        # 10:30 AM in PST (UTC-8) - this is the photo's LOCAL time
        pst = timezone(timedelta(hours=-8))
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=pst)
        result = _to_immich_local_datetime(dt)

        # The result should be 10:30:45 UTC (not 18:30:45 UTC)
        # This preserves the local time appearance
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 10  # Still 10, not converted to 18
        assert result.minute == 30
        assert result.second == 45
        assert result.tzinfo == timezone.utc

    def test_positive_timezone_offset(self):
        """Test with positive timezone offset (e.g., UTC+9 Tokyo)."""
        # 3:00 PM in Tokyo (UTC+9)
        tokyo = timezone(timedelta(hours=9))
        dt = datetime(2024, 6, 20, 15, 0, 0, tzinfo=tokyo)
        result = _to_immich_local_datetime(dt)

        # Should be 15:00:00 UTC (not 06:00:00 UTC)
        assert result is not None
        assert result.hour == 15
        assert result.tzinfo == timezone.utc

    def test_half_hour_timezone_offset(self):
        """Test with half-hour timezone offset (e.g., UTC+5:30 India)."""
        india = timezone(timedelta(hours=5, minutes=30))
        dt = datetime(2024, 3, 10, 14, 45, 0, tzinfo=india)
        result = _to_immich_local_datetime(dt)

        # Should preserve 14:45:00
        assert result is not None
        assert result.hour == 14
        assert result.minute == 45
        assert result.tzinfo == timezone.utc

    def test_naive_datetime(self):
        """Naive datetime (no tzinfo) should be marked as UTC."""
        dt = datetime(2024, 1, 15, 10, 30, 45)  # No tzinfo
        result = _to_immich_local_datetime(dt)

        assert result is not None
        assert result.hour == 10
        assert result.minute == 30
        assert result.tzinfo == timezone.utc


class TestToActualUtc:
    """Tests for _to_actual_utc helper function.

    This function converts datetimes to actual UTC timestamps.
    """

    def test_none_input_returns_none(self):
        """None input should return None."""
        assert _to_actual_utc(None) is None

    def test_utc_datetime_unchanged(self):
        """UTC datetime should remain unchanged."""
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
        result = _to_actual_utc(dt)

        assert result is not None
        assert result.hour == 10
        assert result.minute == 30
        assert result.tzinfo == timezone.utc

    def test_pst_datetime_converted_to_utc(self):
        """PST datetime should be converted to actual UTC.

        10:30 AM PST (UTC-8) becomes 18:30 UTC.
        """
        pst = timezone(timedelta(hours=-8))
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=pst)
        result = _to_actual_utc(dt)

        # 10:30 PST = 18:30 UTC
        assert result is not None
        assert result.hour == 18
        assert result.minute == 30
        assert result.second == 45
        assert result.tzinfo == timezone.utc

    def test_tokyo_datetime_converted_to_utc(self):
        """Tokyo datetime should be converted to actual UTC.

        3:00 PM Tokyo (UTC+9) becomes 06:00 UTC.
        """
        tokyo = timezone(timedelta(hours=9))
        dt = datetime(2024, 6, 20, 15, 0, 0, tzinfo=tokyo)
        result = _to_actual_utc(dt)

        # 15:00 Tokyo = 06:00 UTC
        assert result is not None
        assert result.hour == 6
        assert result.tzinfo == timezone.utc

    def test_naive_datetime_assumed_utc(self):
        """Naive datetime should be assumed to be UTC."""
        dt = datetime(2024, 1, 15, 10, 30, 45)  # No tzinfo
        result = _to_actual_utc(dt)

        assert result is not None
        assert result.hour == 10
        assert result.minute == 30
        assert result.tzinfo == timezone.utc

    def test_date_changes_when_crossing_midnight(self):
        """Verify date changes correctly when UTC conversion crosses midnight."""
        # 11:00 PM in UTC+5 (e.g., Pakistan) on Jan 15
        pkt = timezone(timedelta(hours=5))
        dt = datetime(2024, 1, 15, 23, 0, 0, tzinfo=pkt)
        result = _to_actual_utc(dt)

        # 23:00 PKT = 18:00 UTC (same day)
        assert result is not None
        assert result.day == 15
        assert result.hour == 18

        # 2:00 AM in UTC+5 on Jan 16
        dt2 = datetime(2024, 1, 16, 2, 0, 0, tzinfo=pkt)
        result2 = _to_actual_utc(dt2)

        # 02:00 PKT Jan 16 = 21:00 UTC Jan 15
        assert result2 is not None
        assert result2.day == 15
        assert result2.hour == 21


class TestExtractTimezone:
    """Tests for _extract_timezone helper function.

    This function extracts timezone in Immich's format (e.g., 'UTC+9', 'UTC-8').
    """

    def test_none_input_returns_none(self):
        """None input should return None."""
        assert _extract_timezone(None) is None

    def test_naive_datetime_returns_none(self):
        """Naive datetime (no tzinfo) should return None."""
        dt = datetime(2024, 1, 15, 10, 30, 45)  # No tzinfo
        assert _extract_timezone(dt) is None

    def test_utc_returns_utc_plus_zero(self):
        """UTC timezone should return 'UTC+0'."""
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
        assert _extract_timezone(dt) == "UTC+0"

    def test_positive_offset_no_leading_zero(self):
        """Positive offset should not have leading zero (e.g., 'UTC+9' not 'UTC+09')."""
        tokyo = timezone(timedelta(hours=9))
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=tokyo)
        assert _extract_timezone(dt) == "UTC+9"

    def test_negative_offset_no_leading_zero(self):
        """Negative offset should not have leading zero (e.g., 'UTC-8' not 'UTC-08')."""
        pst = timezone(timedelta(hours=-8))
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=pst)
        assert _extract_timezone(dt) == "UTC-8"

    def test_half_hour_offset_with_minutes(self):
        """Half-hour offset should include minutes (e.g., 'UTC+5:30')."""
        india = timezone(timedelta(hours=5, minutes=30))
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=india)
        assert _extract_timezone(dt) == "UTC+5:30"

    def test_negative_half_hour_offset(self):
        """Negative half-hour offset should format correctly."""
        newfoundland = timezone(timedelta(hours=-3, minutes=-30))
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=newfoundland)
        assert _extract_timezone(dt) == "UTC-3:30"

    def test_large_positive_offset(self):
        """Large positive offset should work (e.g., UTC+14)."""
        kiritimati = timezone(timedelta(hours=14))
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=kiritimati)
        assert _extract_timezone(dt) == "UTC+14"

    def test_large_negative_offset(self):
        """Large negative offset should work (e.g., UTC-12)."""
        baker_island = timezone(timedelta(hours=-12))
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=baker_island)
        assert _extract_timezone(dt) == "UTC-12"

    def test_quarter_hour_offset(self):
        """Quarter-hour offset should include minutes (e.g., 'UTC+5:45')."""
        nepal = timezone(timedelta(hours=5, minutes=45))
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=nepal)
        assert _extract_timezone(dt) == "UTC+5:45"

    def test_parsed_iso_datetime_format(self):
        """Test with datetime parsed from ISO format (simulating SDK deserialization).

        When Pydantic parses '2024-01-15T10:30:00+09:00', the tzinfo
        returns '+09:00' for tzname(), but we should still get 'UTC+9'.
        """
        from pydantic import BaseModel

        class TestModel(BaseModel):
            dt: datetime

        # Simulate JSON round-trip like Gumnut SDK does
        json_str = '{"dt": "2024-01-15T10:30:00+09:00"}'
        parsed = TestModel.model_validate_json(json_str)

        # Even after JSON parsing, we should get Immich's format
        assert _extract_timezone(parsed.dt) == "UTC+9"


class TestGumnutAssetToSyncAssetV1DateHandling:
    """Tests for date handling in gumnut_asset_to_sync_asset_v1."""

    def _create_mock_asset(
        self,
        local_datetime: datetime,
        file_created_at: datetime,
        file_modified_at: datetime,
    ) -> Mock:
        """Create a mock asset with the given dates."""
        asset = Mock()
        asset.id = uuid_to_gumnut_asset_id(TEST_UUID)
        asset.mime_type = "image/jpeg"
        asset.original_file_name = "test.jpg"
        asset.local_datetime = local_datetime
        asset.file_created_at = file_created_at
        asset.file_modified_at = file_modified_at
        asset.checksum = "abc123"
        asset.checksum_sha1 = "sha1checksum"
        return asset

    def test_file_created_at_uses_local_datetime_not_file_created_at(self):
        """fileCreatedAt should use local_datetime (EXIF date), not file_created_at.

        The mobile client displays fileCreatedAt in the timeline. We use the
        EXIF date so photos show when they were taken, not when files were copied.
        """
        # Photo taken in 2020 (EXIF date)
        local_datetime = datetime(2020, 5, 15, 10, 30, 0, tzinfo=timezone.utc)
        # File was copied to new device in 2024 (file system date)
        file_created_at = datetime(2024, 12, 1, 14, 0, 0, tzinfo=timezone.utc)
        file_modified_at = datetime(2024, 12, 1, 14, 0, 0, tzinfo=timezone.utc)

        asset = self._create_mock_asset(
            local_datetime, file_created_at, file_modified_at
        )
        result = gumnut_asset_to_sync_asset_v1(asset, "owner-uuid")

        # fileCreatedAt should be 2020-05-15 (EXIF date), not 2024-12-01 (file date)
        assert result.fileCreatedAt is not None
        assert result.fileCreatedAt.year == 2020
        assert result.fileCreatedAt.month == 5
        assert result.fileCreatedAt.day == 15

    def test_local_datetime_uses_keep_local_time_format(self):
        """localDateTime should use Immich's "keepLocalTime" format.

        localDateTime preserves local time values as UTC so the mobile client
        can display the original local time regardless of viewer timezone.
        """
        # Photo taken at 10:30 AM in PST (UTC-8)
        pst = timezone(timedelta(hours=-8))
        local_datetime = datetime(2024, 1, 15, 10, 30, 0, tzinfo=pst)
        file_created_at = datetime(2024, 1, 15, 18, 30, 0, tzinfo=timezone.utc)
        file_modified_at = file_created_at

        asset = self._create_mock_asset(
            local_datetime, file_created_at, file_modified_at
        )
        result = gumnut_asset_to_sync_asset_v1(asset, "owner-uuid")

        # localDateTime should be 10:30:00 UTC (keepLocalTime - preserves 10:30)
        assert result.localDateTime is not None
        assert result.localDateTime.hour == 10
        assert result.localDateTime.minute == 30
        assert result.localDateTime.tzinfo == timezone.utc

    def test_file_created_at_uses_actual_utc(self):
        """fileCreatedAt should be converted to actual UTC (not keepLocalTime).

        The mobile client applies SQLite's 'localtime' modifier to fileCreatedAt,
        so it must be actual UTC for correct timezone display.

        For a photo taken at 10:30 AM PST: fileCreatedAt = 18:30:00Z (actual UTC),
        then mobile applies 'localtime' to show 10:30 AM in PST timezone.
        """
        # Photo taken at 10:30 AM in PST (UTC-8)
        pst = timezone(timedelta(hours=-8))
        local_datetime = datetime(2024, 1, 15, 10, 30, 0, tzinfo=pst)
        file_created_at = datetime(2024, 1, 15, 18, 30, 0, tzinfo=timezone.utc)
        file_modified_at = file_created_at

        asset = self._create_mock_asset(
            local_datetime, file_created_at, file_modified_at
        )
        result = gumnut_asset_to_sync_asset_v1(asset, "owner-uuid")

        # fileCreatedAt should be 18:30:00 UTC (actual UTC conversion)
        assert result.fileCreatedAt is not None
        assert result.fileCreatedAt.hour == 18  # Converted from 10:30 PST to UTC
        assert result.fileCreatedAt.minute == 30
        assert result.fileCreatedAt.tzinfo == timezone.utc

    def test_file_created_at_and_local_datetime_differ_for_non_utc(self):
        """fileCreatedAt and localDateTime should differ for non-UTC timezones.

        This is the key difference:
        - fileCreatedAt: actual UTC for SQLite localtime conversion
        - localDateTime: keepLocalTime format for preserving local time appearance
        """
        # Photo taken at 3:00 PM in Tokyo (UTC+9)
        tokyo = timezone(timedelta(hours=9))
        local_datetime = datetime(2024, 6, 20, 15, 0, 0, tzinfo=tokyo)
        file_created_at = datetime(2024, 6, 20, 6, 0, 0, tzinfo=timezone.utc)
        file_modified_at = file_created_at

        asset = self._create_mock_asset(
            local_datetime, file_created_at, file_modified_at
        )
        result = gumnut_asset_to_sync_asset_v1(asset, "owner-uuid")

        # localDateTime: 15:00 UTC (keepLocalTime - preserves the 3 PM)
        assert result.localDateTime is not None
        assert result.localDateTime.hour == 15

        # fileCreatedAt: 06:00 UTC (actual UTC - 3 PM Tokyo = 6 AM UTC)
        assert result.fileCreatedAt is not None
        assert result.fileCreatedAt.hour == 6

    def test_file_modified_at_unchanged(self):
        """fileModifiedAt should remain as-is (not converted).

        Only fileCreatedAt and localDateTime need special handling.
        fileModifiedAt can stay as actual UTC.
        """
        local_datetime = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        file_created_at = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        file_modified_at = datetime(2024, 1, 20, 14, 0, 0, tzinfo=timezone.utc)

        asset = self._create_mock_asset(
            local_datetime, file_created_at, file_modified_at
        )
        result = gumnut_asset_to_sync_asset_v1(asset, "owner-uuid")

        # fileModifiedAt should be the same as input
        assert result.fileModifiedAt == file_modified_at

    def test_handles_none_local_datetime(self):
        """Should handle None local_datetime gracefully."""
        asset = Mock()
        asset.id = uuid_to_gumnut_asset_id(TEST_UUID)
        asset.mime_type = "image/jpeg"
        asset.original_file_name = "test.jpg"
        asset.local_datetime = None
        asset.file_created_at = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        asset.file_modified_at = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        asset.checksum = "abc123"
        asset.checksum_sha1 = "sha1checksum"

        result = gumnut_asset_to_sync_asset_v1(asset, "owner-uuid")

        assert result.fileCreatedAt is None
        assert result.localDateTime is None


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


class TestGenerateSyncStream:
    """Tests for generate_sync_stream function."""

    # -------------------------------------------------------------------------
    # Helper methods
    # -------------------------------------------------------------------------

    def _create_mock_user(self, updated_at: datetime) -> Mock:
        """Create a mock Gumnut user."""
        user = Mock()
        user.id = uuid_to_gumnut_user_id(TEST_UUID)
        user.email = "test@example.com"
        user.first_name = "Test"
        user.last_name = "User"
        user.is_superuser = False
        user.updated_at = updated_at
        return user

    def _create_mock_gumnut_client(self, user: Mock) -> Mock:
        """Create a mock Gumnut client with the given user."""
        client = Mock()
        client.users.me.return_value = user
        # Default: no events
        events_response = Mock()
        events_response.data = []
        client.events.get.return_value = events_response
        return client

    async def _collect_stream(self, stream) -> list[dict]:
        """Collect all events from an async generator into a list of dicts."""
        events = []
        async for line in stream:
            events.append(json.loads(line.strip()))
        return events

    def _create_mock_event(self, payload_class, data: Mock) -> Mock:
        """Create a mock event that passes isinstance checks."""
        event = Mock(spec=payload_class)
        event.data = data
        return event

    def _create_mock_asset_data(self, updated_at: datetime) -> Mock:
        """Create mock asset data for AssetEventPayload."""
        asset = Mock()
        asset.id = uuid_to_gumnut_asset_id(TEST_UUID)
        asset.mime_type = "image/jpeg"
        asset.original_file_name = "test.jpg"
        asset.local_datetime = updated_at
        asset.file_created_at = updated_at
        asset.file_modified_at = updated_at
        asset.updated_at = updated_at
        asset.checksum = "abc123"
        asset.checksum_sha1 = "sha1checksum"
        asset.width = 1920
        asset.height = 1080
        return asset

    def _create_mock_album_data(self, updated_at: datetime) -> Mock:
        """Create mock album data for AlbumEventPayload."""
        album = Mock()
        album.id = uuid_to_gumnut_album_id(TEST_UUID)
        album.name = "Test Album"
        album.description = "Test Description"
        album.created_at = updated_at
        album.updated_at = updated_at
        album.album_cover_asset_id = None
        return album

    def _create_mock_album_asset_data(self, updated_at: datetime) -> Mock:
        """Create mock album asset data for AlbumAssetEventPayload."""
        album_asset = Mock()
        album_asset.album_id = uuid_to_gumnut_album_id(TEST_UUID)
        album_asset.asset_id = uuid_to_gumnut_asset_id(TEST_UUID)
        album_asset.updated_at = updated_at
        return album_asset

    def _create_mock_exif_data(self, updated_at: datetime) -> Mock:
        """Create mock exif data for ExifEventPayload."""
        exif = Mock()
        exif.asset_id = uuid_to_gumnut_asset_id(TEST_UUID)
        exif.city = "San Francisco"
        exif.country = "USA"
        exif.state = "California"
        exif.description = None
        exif.original_datetime = updated_at
        exif.modified_datetime = None
        exif.exposure_time = 0.01
        exif.f_number = 2.8
        exif.focal_length = 50.0
        exif.iso = 100
        exif.latitude = 37.7749
        exif.longitude = -122.4194
        exif.lens_model = "50mm f/1.8"
        exif.make = "Canon"
        exif.model = "EOS R5"
        exif.orientation = 1
        exif.profile_description = None
        exif.projection_type = None
        exif.rating = None
        exif.fps = None
        exif.updated_at = updated_at
        return exif

    def _create_mock_person_data(self, updated_at: datetime) -> Mock:
        """Create mock person data for PersonEventPayload."""
        person = Mock()
        person.id = uuid_to_gumnut_person_id(TEST_UUID)
        person.name = "Test Person"
        person.is_favorite = False
        person.is_hidden = False
        person.created_at = updated_at
        person.updated_at = updated_at
        return person

    def _create_mock_face_data(self, updated_at: datetime) -> Mock:
        """Create mock face data for FaceEventPayload."""
        face = Mock()
        face.id = uuid_to_gumnut_face_id(TEST_UUID)
        face.asset_id = uuid_to_gumnut_asset_id(TEST_UUID)
        face.person_id = uuid_to_gumnut_person_id(TEST_UUID)
        face.bounding_box = {"x": 100, "y": 100, "w": 50, "h": 50}
        face.updated_at = updated_at
        return face

    # -------------------------------------------------------------------------
    # Core behavior tests
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_always_streams_sync_complete(self):
        """SyncCompleteV1 is always streamed at the end."""
        mock_user = self._create_mock_user(datetime.now(timezone.utc))
        mock_client = self._create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"
        assert events[0]["data"] == {}

    @pytest.mark.anyio
    async def test_event_format_includes_ack(self):
        """Each event includes an ack string for checkpointing."""
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(user_updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        auth_event = events[0]
        assert "ack" in auth_event
        assert auth_event["ack"].startswith("AuthUserV1|")
        assert auth_event["ack"].endswith("|")

    @pytest.mark.anyio
    async def test_streams_error_on_exception(self):
        """Error event is streamed when an exception occurs."""
        mock_client = Mock()
        mock_client.users.me.side_effect = Exception("API error")

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 1
        assert events[0]["type"] == "Error"
        assert "message" in events[0]["data"]

    # -------------------------------------------------------------------------
    # User entity tests (special cases - not from events API)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_streams_auth_user_when_requested(self):
        """Auth user is streamed when AuthUsersV1 is requested."""
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(user_updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AuthUserV1"
        assert events[0]["data"]["email"] == "test@example.com"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_user_when_requested(self):
        """User is streamed when UsersV1 is requested."""
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(user_updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.UsersV1])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "UserV1"
        assert events[0]["data"]["email"] == "test@example.com"
        assert events[1]["type"] == "SyncCompleteV1"

    # -------------------------------------------------------------------------
    # Checkpoint/delta sync tests
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_skips_entity_when_not_updated_since_checkpoint(self):
        """Entity is skipped when checkpoint is newer than updated_at."""
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        checkpoint_time = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(user_updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map = {SyncEntityType.AuthUserV1: checkpoint_time}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_entity_when_updated_after_checkpoint(self):
        """Entity is streamed when updated_at is after checkpoint."""
        user_updated_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        checkpoint_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(user_updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map = {SyncEntityType.AuthUserV1: checkpoint_time}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AuthUserV1"

    # -------------------------------------------------------------------------
    # Events API entity tests (in processing order)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_streams_assets_when_requested(self):
        """Assets are streamed when AssetsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        asset_data = self._create_mock_asset_data(updated_at)
        asset_event = self._create_mock_event(AssetEventPayload, asset_data)

        events_response = Mock()
        events_response.data = [asset_event]
        mock_client.events.get.return_value = events_response

        request = SyncStreamDto(types=[SyncRequestType.AssetsV1])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetV1"
        assert events[0]["data"]["originalFileName"] == "test.jpg"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_albums_when_requested(self):
        """Albums are streamed when AlbumsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        album_data = self._create_mock_album_data(updated_at)
        album_event = self._create_mock_event(AlbumEventPayload, album_data)

        events_response = Mock()
        events_response.data = [album_event]
        mock_client.events.get.return_value = events_response

        request = SyncStreamDto(types=[SyncRequestType.AlbumsV1])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AlbumV1"
        assert events[0]["data"]["name"] == "Test Album"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_album_assets_when_requested(self):
        """Album-to-asset mappings are streamed when AlbumToAssetsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        album_asset_data = self._create_mock_album_asset_data(updated_at)
        album_asset_event = self._create_mock_event(
            AlbumAssetEventPayload, album_asset_data
        )

        events_response = Mock()
        events_response.data = [album_asset_event]
        mock_client.events.get.return_value = events_response

        request = SyncStreamDto(types=[SyncRequestType.AlbumToAssetsV1])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AlbumToAssetV1"
        assert "albumId" in events[0]["data"]
        assert "assetId" in events[0]["data"]
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_exif_when_requested(self):
        """EXIF data is streamed when AssetExifsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        exif_data = self._create_mock_exif_data(updated_at)
        exif_event = self._create_mock_event(ExifEventPayload, exif_data)

        events_response = Mock()
        events_response.data = [exif_event]
        mock_client.events.get.return_value = events_response

        request = SyncStreamDto(types=[SyncRequestType.AssetExifsV1])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetExifV1"
        assert events[0]["data"]["city"] == "San Francisco"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_people_when_requested(self):
        """People are streamed when PeopleV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        person_data = self._create_mock_person_data(updated_at)
        person_event = self._create_mock_event(PersonEventPayload, person_data)

        events_response = Mock()
        events_response.data = [person_event]
        mock_client.events.get.return_value = events_response

        request = SyncStreamDto(types=[SyncRequestType.PeopleV1])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "PersonV1"
        assert events[0]["data"]["name"] == "Test Person"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_faces_when_requested(self):
        """Faces are streamed when AssetFacesV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        face_data = self._create_mock_face_data(updated_at)
        face_event = self._create_mock_event(FaceEventPayload, face_data)

        events_response = Mock()
        events_response.data = [face_event]
        mock_client.events.get.return_value = events_response

        request = SyncStreamDto(types=[SyncRequestType.AssetFacesV1])
        checkpoint_map: dict[SyncEntityType, datetime] = {}

        events = await self._collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetFaceV1"
        assert "boundingBoxX1" in events[0]["data"]
        assert events[1]["type"] == "SyncCompleteV1"


class TestGetSyncStreamEndpoint:
    """Tests for the get_sync_stream endpoint."""

    def _create_mock_user(self, updated_at: datetime) -> Mock:
        """Create a mock Gumnut user."""
        user = Mock()
        user.id = uuid_to_gumnut_user_id(TEST_UUID)
        user.email = "test@example.com"
        user.first_name = "Test"
        user.last_name = "User"
        user.is_superuser = False
        user.updated_at = updated_at
        return user

    def _create_mock_gumnut_client(self, user: Mock) -> Mock:
        """Create a mock Gumnut client."""
        client = Mock()
        client.users.me.return_value = user
        events_response = Mock()
        events_response.data = []
        client.events.get.return_value = events_response
        return client

    @pytest.mark.anyio
    async def test_returns_streaming_response_with_correct_media_type(self):
        """Endpoint returns StreamingResponse with jsonlines media type."""
        from fastapi.responses import StreamingResponse

        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        mock_request = Mock()
        mock_request.state.session_token = None

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_checkpoint_store.get_all.return_value = []

        request = SyncStreamDto(types=[])

        result = await get_sync_stream(
            request=request,
            http_request=mock_request,
            gumnut_client=mock_client,
            checkpoint_store=mock_checkpoint_store,
        )

        assert isinstance(result, StreamingResponse)
        assert result.media_type == "application/jsonlines+json"

    @pytest.mark.anyio
    async def test_loads_checkpoints_when_session_token_present(self):
        """Checkpoints are loaded from store when session token is valid."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = self._create_mock_user(updated_at)
        mock_client = self._create_mock_gumnut_client(mock_user)

        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        # Create checkpoint that will cause auth user to be skipped
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AuthUserV1,
            last_synced_at=datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc),
        )
        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_checkpoint_store.get_all.return_value = [checkpoint]

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])

        result = await get_sync_stream(
            request=request,
            http_request=mock_request,
            gumnut_client=mock_client,
            checkpoint_store=mock_checkpoint_store,
        )

        # Verify checkpoint store was called with correct session UUID
        mock_checkpoint_store.get_all.assert_called_once_with(TEST_SESSION_UUID)

        # Consume stream and verify auth user was skipped due to checkpoint
        events = []
        async for chunk in result.body_iterator:
            line = bytes(chunk).decode() if not isinstance(chunk, str) else chunk
            events.append(json.loads(line.strip()))

        # Only SyncCompleteV1 (auth user skipped because checkpoint is newer)
        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"


class TestGetSyncAck:
    """Tests for the get_sync_ack endpoint."""

    @pytest.mark.anyio
    async def test_returns_checkpoints_as_ack_dtos(self):
        """Stored checkpoints are returned as SyncAckDto list."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        checkpoint_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AssetV1,
            last_synced_at=checkpoint_time,
            updated_at=checkpoint_time,
        )
        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_checkpoint_store.get_all.return_value = [checkpoint]

        result = await get_sync_ack(
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        assert len(result) == 1
        assert result[0].type == SyncEntityType.AssetV1
        assert result[0].ack == f"AssetV1|{checkpoint_time.isoformat()}|"
        mock_checkpoint_store.get_all.assert_called_once_with(TEST_SESSION_UUID)

    @pytest.mark.anyio
    async def test_returns_empty_list_when_no_checkpoints(self):
        """Returns empty list when no checkpoints exist for session."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_checkpoint_store.get_all.return_value = []

        result = await get_sync_ack(
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        assert result == []


class TestSendSyncAck:
    """Tests for the send_sync_ack endpoint."""

    @pytest.mark.anyio
    async def test_stores_valid_checkpoints(self):
        """Valid acks are parsed and stored as checkpoints."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_session_store = AsyncMock(spec=SessionStore)

        checkpoint_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        request = SyncAckSetDto(acks=[f"AssetV1|{checkpoint_time.isoformat()}|"])

        await send_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # Verify checkpoint was stored
        mock_checkpoint_store.set_many.assert_called_once()
        call_args = mock_checkpoint_store.set_many.call_args
        assert call_args[0][0] == TEST_SESSION_UUID
        checkpoints = call_args[0][1]
        assert len(checkpoints) == 1
        assert checkpoints[0] == (SyncEntityType.AssetV1, checkpoint_time)

        # Verify session activity was updated
        mock_session_store.update_activity.assert_called_once_with(
            str(TEST_SESSION_UUID)
        )

    @pytest.mark.anyio
    async def test_handles_sync_reset_ack(self):
        """SyncResetV1 ack clears pending reset flag and deletes all checkpoints."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_session_store = AsyncMock(spec=SessionStore)

        request = SyncAckSetDto(acks=["SyncResetV1||"])

        await send_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # Verify sync reset was handled
        mock_session_store.set_pending_sync_reset.assert_called_once_with(
            str(TEST_SESSION_UUID), False
        )
        mock_checkpoint_store.delete_all.assert_called_once_with(TEST_SESSION_UUID)
        mock_session_store.update_activity.assert_called_once_with(
            str(TEST_SESSION_UUID)
        )

        # Verify set_many was NOT called (early return after reset)
        mock_checkpoint_store.set_many.assert_not_called()

    @pytest.mark.anyio
    async def test_skips_malformed_acks(self):
        """Malformed acks are skipped, valid acks are still processed."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_session_store = AsyncMock(spec=SessionStore)

        checkpoint_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        request = SyncAckSetDto(
            acks=[
                "malformed",  # Too few parts - skipped
                f"AssetV1|{checkpoint_time.isoformat()}|",  # Valid
            ]
        )

        await send_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # Only the valid checkpoint should be stored
        call_args = mock_checkpoint_store.set_many.call_args
        checkpoints = call_args[0][1]
        assert len(checkpoints) == 1
        assert checkpoints[0] == (SyncEntityType.AssetV1, checkpoint_time)

    @pytest.mark.anyio
    async def test_does_not_store_when_all_acks_malformed(self):
        """When all acks are malformed, set_many is not called."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_session_store = AsyncMock(spec=SessionStore)

        # Use valid entity types but invalid timestamps (malformed acks that get skipped)
        request = SyncAckSetDto(acks=["malformed", "AssetV1|not-a-valid-timestamp|"])

        await send_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # set_many should not be called since no valid checkpoints
        mock_checkpoint_store.set_many.assert_not_called()


class TestDeleteSyncAck:
    """Tests for the delete_sync_ack endpoint."""

    @pytest.mark.anyio
    async def test_deletes_specific_checkpoint_types(self):
        """Deletes only the specified checkpoint types."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)

        request = SyncAckDeleteDto(
            types=[SyncEntityType.AssetV1, SyncEntityType.AlbumV1]
        )

        await delete_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        mock_checkpoint_store.delete.assert_called_once_with(
            TEST_SESSION_UUID, [SyncEntityType.AssetV1, SyncEntityType.AlbumV1]
        )
        mock_checkpoint_store.delete_all.assert_not_called()

    @pytest.mark.anyio
    async def test_does_nothing_when_types_empty(self):
        """Does nothing when types list is empty (matches Immich behavior)."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)

        request = SyncAckDeleteDto(types=[])

        await delete_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        # Empty list = no-op, matching Immich's behavior
        mock_checkpoint_store.delete_all.assert_not_called()
        mock_checkpoint_store.delete.assert_not_called()

    @pytest.mark.anyio
    async def test_deletes_all_checkpoints_when_types_none(self):
        """Deletes all checkpoints when types is None."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)

        request = SyncAckDeleteDto(types=None)

        await delete_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        mock_checkpoint_store.delete_all.assert_called_once_with(TEST_SESSION_UUID)
        mock_checkpoint_store.delete.assert_not_called()
