"""Tests for timeline.py endpoints."""

import pytest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from routers.api.timeline import (
    get_time_buckets,
    get_time_bucket,
)
from routers.immich_models import (
    AssetOrder,
    AssetVisibility,
)
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id


def call_get_time_buckets(**kwargs):
    """Helper function to call get_time_buckets with proper None defaults for Query parameters."""
    defaults = {
        "albumId": None,
        "isFavorite": None,
        "isTrashed": None,
        "key": None,
        "order": None,
        "personId": None,
        "slug": None,
        "tagId": None,
        "userId": None,
        "visibility": None,
        "withCoordinates": None,
        "withPartners": None,
        "withStacked": None,
    }
    defaults.update(kwargs)
    return get_time_buckets(**defaults)  # type: ignore


def call_get_time_bucket(timeBucket, **kwargs):
    """Helper function to call get_time_bucket with proper None defaults for Query parameters."""
    defaults = {
        "albumId": None,
        "isFavorite": None,
        "isTrashed": None,
        "key": None,
        "order": None,
        "personId": None,
        "slug": None,
        "tagId": None,
        "userId": None,
        "visibility": None,
        "withCoordinates": None,
        "withPartners": None,
        "withStacked": None,
    }
    defaults.update(kwargs)
    return get_time_bucket(timeBucket, **defaults)  # type: ignore


class TestGetTimeBuckets:
    """Test the get_time_buckets endpoint."""

    @pytest.mark.anyio
    async def test_get_time_buckets_success(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """Test successful retrieval of time buckets."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets with different dates
            assets = multiple_gumnut_assets
            assets[0].local_datetime = datetime(
                2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc
            )
            assets[0].created_at = assets[0].local_datetime
            assets[1].local_datetime = datetime(
                2024, 2, 20, 14, 0, 0, tzinfo=timezone.utc
            )
            assets[1].created_at = assets[1].local_datetime
            assets[2].local_datetime = datetime(
                2024, 1, 25, 16, 0, 0, tzinfo=timezone.utc
            )
            assets[2].created_at = assets[2].local_datetime

            mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

            # Execute
            result = await call_get_time_buckets()

            # Assert
            assert len(result) == 2  # Two different months
            # Should be sorted descending by default
            assert result[0].timeBucket == "2024-02-01"  # February (later month first)
            assert result[0].count == 1
            assert result[1].timeBucket == "2024-01-01"  # January
            assert result[1].count == 2  # Two assets in January
            mock_client.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_time_buckets_with_album_id(
        self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid
    ):
        """Test time buckets with album filter."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets
            assets = multiple_gumnut_assets
            assets[0].local_datetime = datetime(
                2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc
            )
            assets[0].created_at = assets[0].local_datetime

            mock_client.albums.assets.list.return_value = mock_sync_cursor_page(
                [assets[0]]
            )

            # Execute
            result = await call_get_time_buckets(albumId=sample_uuid)

            # Assert
            assert len(result) == 1
            assert result[0].timeBucket == "2024-01-01"
            assert result[0].count == 1
            mock_client.albums.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_time_buckets_with_person_id(
        self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid
    ):
        """Test time buckets with person filter."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets
            assets = multiple_gumnut_assets
            assets[0].local_datetime = datetime(
                2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc
            )
            assets[0].created_at = assets[0].local_datetime

            mock_client.assets.list.return_value = mock_sync_cursor_page([assets[0]])

            # Execute
            result = await call_get_time_buckets(personId=sample_uuid)

            # Assert
            assert len(result) == 1
            assert result[0].timeBucket == "2024-01-01"
            assert result[0].count == 1
            # Should be called with person_id parameter
            mock_client.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_time_buckets_ascending_order(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """Test time buckets with ascending order."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets with different dates
            assets = multiple_gumnut_assets
            assets[0].local_datetime = datetime(
                2024, 2, 15, 10, 0, 0, tzinfo=timezone.utc
            )
            assets[0].created_at = assets[0].local_datetime
            assets[1].local_datetime = datetime(
                2024, 1, 20, 14, 0, 0, tzinfo=timezone.utc
            )
            assets[1].created_at = assets[1].local_datetime

            mock_client.assets.list.return_value = mock_sync_cursor_page(assets[:2])

            # Execute
            result = await call_get_time_buckets(order=AssetOrder.asc)

            # Assert
            assert len(result) == 2
            # Should be sorted ascending
            assert result[0].timeBucket == "2024-01-01"  # January first in ascending
            assert result[1].timeBucket == "2024-02-01"  # February second in ascending

    @pytest.mark.anyio
    async def test_get_time_buckets_filtered_out_conditions(self):
        """Test that certain conditions return empty list immediately."""
        # Test isFavorite=True
        result = await call_get_time_buckets(isFavorite=True)
        assert result == []

        # Test isTrashed=True
        result = await call_get_time_buckets(isTrashed=True)
        assert result == []

        # Test non-timeline visibility
        result = await call_get_time_buckets(visibility=AssetVisibility.archive)
        assert result == []

    @pytest.mark.anyio
    async def test_get_time_buckets_string_datetime(self, mock_sync_cursor_page):
        """Test handling of string datetime values."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Create mock asset with string datetime
            mock_asset = Mock()
            mock_asset.local_datetime = "2024-01-15T10:00:00Z"
            mock_asset.created_at = "2024-01-15T10:00:00Z"

            mock_client.assets.list.return_value = mock_sync_cursor_page([mock_asset])

            # Execute
            result = await call_get_time_buckets()

            # Assert
            assert len(result) == 1
            assert result[0].timeBucket == "2024-01-01"
            assert result[0].count == 1

    @pytest.mark.anyio
    async def test_get_time_buckets_empty_assets(self, mock_sync_cursor_page):
        """Test time buckets with no assets."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.return_value = mock_sync_cursor_page([])

            # Execute
            result = await call_get_time_buckets()

            # Assert
            assert result == []

    @pytest.mark.anyio
    async def test_get_time_buckets_gumnut_error(self):
        """Test handling of Gumnut API errors."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.side_effect = Exception("API Error")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await call_get_time_buckets()

            assert exc_info.value.status_code == 500
            assert "Failed to fetch timeline buckets" in str(exc_info.value.detail)

    @pytest.mark.anyio
    async def test_get_time_buckets_auth_error(self):
        """Test handling of authentication errors."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.side_effect = Exception("401 Invalid API key")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await call_get_time_buckets()

            assert exc_info.value.status_code == 401


class TestGetTimeBucket:
    """Test the get_time_bucket endpoint."""

    @pytest.mark.anyio
    async def test_get_time_bucket_success(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """Test successful retrieval of time bucket assets."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets - some in January 2024, some in February
            assets = multiple_gumnut_assets
            # Asset 0: January 2024 (should be included)
            assets[0].id = uuid_to_gumnut_asset_id(uuid4())
            assets[0].local_datetime = datetime(
                2024, 1, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=-5))
            )
            assets[0].created_at = assets[0].local_datetime
            assets[0].mime_type = "image/jpeg"
            assets[0].width = 1920
            assets[0].height = 1280

            # Asset 1: February 2024 (should NOT be included)
            assets[1].id = uuid_to_gumnut_asset_id(uuid4())
            assets[1].local_datetime = datetime(
                2024, 2, 20, 14, 0, 0, tzinfo=timezone.utc
            )
            assets[1].created_at = assets[1].local_datetime
            assets[1].mime_type = "video/mp4"
            assets[1].width = 1920
            assets[1].height = 1080

            # Asset 2: January 2024 (should be included)
            assets[2].id = uuid_to_gumnut_asset_id(uuid4())
            assets[2].local_datetime = datetime(
                2024, 1, 25, 16, 0, 0, tzinfo=timezone(timedelta(hours=2))
            )
            assets[2].created_at = assets[2].local_datetime
            assets[2].mime_type = "image/png"
            assets[2].width = 1080
            assets[2].height = 1080

            mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

            # Mock get_current_user_id
            with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute - request January 2024 bucket
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert
                assert isinstance(result, dict)

                # Should have 2 assets (asset-jan-1 and asset-jan-2)
                assert len(result["id"]) == 2
                assert len(result["fileCreatedAt"]) == 2
                assert len(result["isImage"]) == 2
                assert len(result["ratio"]) == 2

                # Check that image/video detection works
                assert result["isImage"][0] is True  # image/jpeg
                assert result["isImage"][1] is True  # image/png

                # Check aspect ratios (calculated from width/height)
                assert result["ratio"][0] == 1920 / 1280  # 1.5
                assert result["ratio"][1] == 1080 / 1080  # 1.0

                # Check timezone offsets
                assert result["localOffsetHours"][0] == -5
                assert result["localOffsetHours"][1] == 2

                # Check fixed fields
                assert all(fav is False for fav in result["isFavorite"])
                assert all(trash is False for trash in result["isTrashed"])
                assert all(
                    vis == AssetVisibility.timeline for vis in result["visibility"]
                )

    @pytest.mark.anyio
    async def test_get_time_bucket_with_album_id(
        self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid
    ):
        """Test time bucket with album filter."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets
            assets = multiple_gumnut_assets
            assets[0].id = uuid_to_gumnut_asset_id(uuid4())
            assets[0].local_datetime = datetime(
                2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc
            )
            assets[0].created_at = assets[0].local_datetime
            assets[0].mime_type = "image/jpeg"
            assets[0].width = 1920
            assets[0].height = 1080

            mock_client.albums.assets.list.return_value = mock_sync_cursor_page(
                [assets[0]]
            )

            # Mock get_current_user_id
            with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
                mock_user_id.return_value = sample_uuid

                # Execute
                result = await call_get_time_bucket(
                    timeBucket="2024-01-01T00:00:00", albumId=sample_uuid
                )

                # Assert
                assert len(result["id"]) == 1
                mock_client.albums.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_time_bucket_with_person_id(
        self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid
    ):
        """Test time bucket with person filter."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets
            assets = multiple_gumnut_assets
            assets[0].id = uuid_to_gumnut_asset_id(uuid4())
            assets[0].local_datetime = datetime(
                2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc
            )
            assets[0].created_at = assets[0].local_datetime
            assets[0].mime_type = "image/jpeg"
            assets[0].width = 1920
            assets[0].height = 1080

            mock_client.assets.list.return_value = mock_sync_cursor_page([assets[0]])

            # Mock get_current_user_id
            with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
                mock_user_id.return_value = sample_uuid

                # Execute
                result = await call_get_time_bucket(
                    timeBucket="2024-01-01T00:00:00", personId=sample_uuid
                )

                # Assert
                assert len(result["id"]) == 1
                # Should be called with person_id parameter
                mock_client.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_time_bucket_no_matching_assets(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """Test time bucket with no assets matching the time."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets from February 2024
            assets = multiple_gumnut_assets
            assets[0].local_datetime = datetime(
                2024, 2, 15, 10, 0, 0, tzinfo=timezone.utc
            )
            assets[0].created_at = assets[0].local_datetime

            mock_client.assets.list.return_value = mock_sync_cursor_page(assets[:1])

            # Mock get_current_user_id
            with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute - request January 2024 bucket (no matching assets)
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert
                assert len(result["id"]) == 0
                assert len(result["fileCreatedAt"]) == 0
                assert len(result["isImage"]) == 0

    @pytest.mark.anyio
    async def test_get_time_bucket_with_non_utc_timezone(self, mock_sync_cursor_page):
        """Test handling of assets with non-UTC timezone offsets."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Create mock asset with timezone offset
            mock_asset = Mock()
            mock_asset.id = uuid_to_gumnut_asset_id(uuid4())
            # UTC+10 (Australian Eastern Standard Time)
            mock_asset.local_datetime = datetime(
                2024, 1, 15, 20, 0, 0, tzinfo=timezone(timedelta(hours=10))
            )
            mock_asset.created_at = mock_asset.local_datetime
            mock_asset.mime_type = "image/jpeg"
            mock_asset.width = 1920
            mock_asset.height = 1280

            mock_client.assets.list.return_value = mock_sync_cursor_page([mock_asset])

            # Mock get_current_user_id
            with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert
                assert len(result["id"]) == 1
                assert result["ratio"][0] == 1920 / 1280  # 1.5
                assert result["localOffsetHours"][0] == 10  # UTC+10
                assert result["isImage"][0] is True

    @pytest.mark.anyio
    async def test_get_time_bucket_missing_attributes(self, mock_sync_cursor_page):
        """Test handling of assets with missing attributes."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Create mock asset with minimal attributes
            mock_asset = Mock()
            mock_asset.id = uuid_to_gumnut_asset_id(uuid4())
            mock_asset.local_datetime = datetime(
                2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc
            )
            mock_asset.created_at = None
            # Set defaults for missing attributes to avoid Mock conversion errors
            mock_asset.mime_type = ""
            mock_asset.width = None
            mock_asset.height = None

            mock_client.assets.list.return_value = mock_sync_cursor_page([mock_asset])

            # Mock get_current_user_id
            with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert
                assert len(result["id"]) == 1
                # Should use defaults for missing attributes
                assert result["ratio"][0] == 1.0  # Default aspect ratio
                assert result["localOffsetHours"][0] == 0  # Default offset (UTC)
                assert (
                    result["isImage"][0] is False
                )  # Empty mime_type doesn't start with "image/"

    @pytest.mark.anyio
    async def test_get_time_bucket_invalid_date_format(self):
        """Test handling of invalid timeBucket format."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.return_value = []

            # Execute & Assert - invalid date format should raise exception
            with pytest.raises(Exception):
                await call_get_time_bucket(timeBucket="invalid-date-format")

    @pytest.mark.anyio
    async def test_get_time_bucket_gumnut_error(self):
        """Test handling of Gumnut API errors."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.side_effect = Exception("API Error")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

            assert exc_info.value.status_code == 500
            assert "Failed to fetch timeline bucket" in str(exc_info.value.detail)

    @pytest.mark.anyio
    async def test_get_time_bucket_auth_error(self):
        """Test handling of authentication errors."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.side_effect = Exception("401 Invalid API key")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

            assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_get_time_bucket_timezone_offsets(self, mock_sync_cursor_page):
        """Test timezone offset calculation for assets with different timezones."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Create assets with different timezone offsets
            assets = []

            # Asset 1: UTC+5:30 (India Standard Time)
            asset1 = Mock()
            asset1.id = uuid_to_gumnut_asset_id(uuid4())
            asset1.local_datetime = datetime(
                2024, 1, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
            )
            asset1.created_at = asset1.local_datetime
            asset1.mime_type = "image/jpeg"
            asset1.width = 1920
            asset1.height = 1080
            assets.append(asset1)

            # Asset 2: UTC-8:00 (Pacific Standard Time)
            asset2 = Mock()
            asset2.id = uuid_to_gumnut_asset_id(uuid4())
            asset2.local_datetime = datetime(
                2024, 1, 15, 14, 0, 0, tzinfo=timezone(timedelta(hours=-8))
            )
            asset2.created_at = asset2.local_datetime
            asset2.mime_type = "image/png"
            asset2.width = 1024
            asset2.height = 768
            assets.append(asset2)

            # Asset 3: UTC+0:00 (UTC/GMT)
            asset3 = Mock()
            asset3.id = uuid_to_gumnut_asset_id(uuid4())
            asset3.local_datetime = datetime(2024, 1, 15, 18, 0, 0, tzinfo=timezone.utc)
            asset3.created_at = asset3.local_datetime
            asset3.mime_type = "video/mp4"
            asset3.width = 3840
            asset3.height = 2160
            assets.append(asset3)

            # Asset 4: UTC-3:30 (Newfoundland Standard Time - half hour offset)
            asset4 = Mock()
            asset4.id = uuid_to_gumnut_asset_id(uuid4())
            asset4.local_datetime = datetime(
                2024, 1, 15, 20, 0, 0, tzinfo=timezone(timedelta(hours=-3, minutes=-30))
            )
            asset4.created_at = asset4.local_datetime
            asset4.mime_type = "image/jpeg"
            asset4.width = 1600
            asset4.height = 1200
            assets.append(asset4)

            mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

            # Mock get_current_user_id
            with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert - check timezone offsets are correctly calculated
                assert len(result["id"]) == 4
                assert len(result["localOffsetHours"]) == 4

                # UTC+5:30 should be 5 hours (integer division)
                assert result["localOffsetHours"][0] == 5

                # UTC-8:00 should be -8 hours
                assert result["localOffsetHours"][1] == -8

                # UTC should be 0 hours
                assert result["localOffsetHours"][2] == 0

                # UTC-3:30 should be -3 hours (integer division truncates)
                assert result["localOffsetHours"][3] == -3

    @pytest.mark.anyio
    async def test_get_time_bucket_no_timezone_info(self, mock_sync_cursor_page):
        """Test timezone offset calculation for assets without timezone info (naive datetime)."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Create assets without timezone info (naive datetime)
            assets = []

            # Asset 1: Naive datetime (no tzinfo)
            asset1 = Mock()
            asset1.id = uuid_to_gumnut_asset_id(uuid4())
            asset1.local_datetime = datetime(2024, 1, 15, 10, 0, 0)  # No tzinfo
            asset1.created_at = asset1.local_datetime
            asset1.mime_type = "image/jpeg"
            asset1.width = 1920
            asset1.height = 1080
            assets.append(asset1)

            # Asset 2: Another naive datetime
            asset2 = Mock()
            asset2.id = uuid_to_gumnut_asset_id(uuid4())
            asset2.local_datetime = datetime(2024, 1, 15, 14, 0, 0)  # No tzinfo
            asset2.created_at = asset2.local_datetime
            asset2.mime_type = "image/png"
            asset2.width = 1024
            asset2.height = 768
            assets.append(asset2)

            mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

            # Mock get_current_user_id
            with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert - naive datetimes should default to 0 offset
                assert len(result["id"]) == 2
                assert len(result["localOffsetHours"]) == 2

                # Both assets should have 0 offset (no timezone info)
                assert result["localOffsetHours"][0] == 0
                assert result["localOffsetHours"][1] == 0

    @pytest.mark.anyio
    async def test_get_time_bucket_mixed_timezone_info(self, mock_sync_cursor_page):
        """Test timezone offset calculation for mixed assets (some with tzinfo, some without)."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.timeline.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Create mixed assets - some with timezone, some without
            assets = []

            # Asset 1: With timezone (UTC+2)
            asset1 = Mock()
            asset1.id = uuid_to_gumnut_asset_id(uuid4())
            asset1.local_datetime = datetime(
                2024, 1, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=2))
            )
            asset1.created_at = asset1.local_datetime
            asset1.mime_type = "image/jpeg"
            asset1.width = 1920
            asset1.height = 1080
            assets.append(asset1)

            # Asset 2: Without timezone (naive)
            asset2 = Mock()
            asset2.id = uuid_to_gumnut_asset_id(uuid4())
            asset2.local_datetime = datetime(2024, 1, 15, 14, 0, 0)  # No tzinfo
            asset2.created_at = asset2.local_datetime
            asset2.mime_type = "image/png"
            asset2.width = 1024
            asset2.height = 768
            assets.append(asset2)

            # Asset 3: With timezone (UTC-5)
            asset3 = Mock()
            asset3.id = uuid_to_gumnut_asset_id(uuid4())
            asset3.local_datetime = datetime(
                2024, 1, 15, 18, 0, 0, tzinfo=timezone(timedelta(hours=-5))
            )
            asset3.created_at = asset3.local_datetime
            asset3.mime_type = "video/mp4"
            asset3.width = 3840
            asset3.height = 2160
            assets.append(asset3)

            # Asset 4: Without timezone (naive)
            asset4 = Mock()
            asset4.id = uuid_to_gumnut_asset_id(uuid4())
            asset4.local_datetime = datetime(2024, 1, 15, 20, 0, 0)  # No tzinfo
            asset4.created_at = asset4.local_datetime
            asset4.mime_type = "image/jpeg"
            asset4.width = 1600
            asset4.height = 1200
            assets.append(asset4)

            mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

            # Mock get_current_user_id
            with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert - check mixed timezone handling
                assert len(result["id"]) == 4
                assert len(result["localOffsetHours"]) == 4

                # Asset 1: UTC+2
                assert result["localOffsetHours"][0] == 2

                # Asset 2: Naive (no tzinfo) -> 0
                assert result["localOffsetHours"][1] == 0

                # Asset 3: UTC-5
                assert result["localOffsetHours"][2] == -5

                # Asset 4: Naive (no tzinfo) -> 0
                assert result["localOffsetHours"][3] == 0
