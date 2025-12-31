"""Tests for sync.py date conversion functions."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import UUID

from routers.api.sync import (
    _extract_timezone,
    _to_actual_utc,
    _to_immich_local_datetime,
    gumnut_asset_to_sync_asset_v1,
)
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id

TEST_UUID = UUID("12345678-1234-1234-1234-123456789abc")


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
