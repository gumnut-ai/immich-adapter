"""Tests for timeline.py endpoints."""

import pytest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from uuid import uuid4
from datetime import datetime, timezone

from routers.api.timeline import (
    get_time_buckets,
    get_time_bucket,
)
from routers.immich_models import (
    AssetOrder,
    AssetVisibility,
)


def call_get_time_buckets(**kwargs):
    """Helper function to call get_time_buckets with proper None defaults for Query parameters."""
    defaults = {
        'albumId': None,
        'isFavorite': None,
        'isTrashed': None,
        'key': None,
        'order': None,
        'personId': None,
        'slug': None,
        'tagId': None,
        'userId': None,
        'visibility': None,
        'withCoordinates': None,
        'withPartners': None,
        'withStacked': None,
    }
    defaults.update(kwargs)
    return get_time_buckets(**defaults) # type: ignore


def call_get_time_bucket(timeBucket, **kwargs):
    """Helper function to call get_time_bucket with proper None defaults for Query parameters."""
    defaults = {
        'albumId': None,
        'isFavorite': None,
        'isTrashed': None,
        'key': None,
        'order': None,
        'personId': None,
        'slug': None,
        'tagId': None,
        'userId': None,
        'visibility': None,
        'withCoordinates': None,
        'withPartners': None,
        'withStacked': None,
    }
    defaults.update(kwargs)
    return get_time_bucket(timeBucket, **defaults) # type: ignore


class TestGetTimeBuckets:
    """Test the get_time_buckets endpoint."""

    @pytest.mark.anyio
    async def test_get_time_buckets_success(self, multiple_gumnut_assets, mock_sync_cursor_page):
        """Test successful retrieval of time buckets."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets with different dates
            assets = multiple_gumnut_assets
            assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
            assets[0].created_at = assets[0].local_datetime
            assets[1].local_datetime = datetime(2024, 2, 20, 14, 0, 0, tzinfo=timezone.utc)
            assets[1].created_at = assets[1].local_datetime
            assets[2].local_datetime = datetime(2024, 1, 25, 16, 0, 0, tzinfo=timezone.utc)
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
    async def test_get_time_buckets_with_album_id(self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid):
        """Test time buckets with album filter."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets
            assets = multiple_gumnut_assets
            assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
            assets[0].created_at = assets[0].local_datetime

            mock_client.albums.assets.list.return_value = mock_sync_cursor_page([assets[0]])

            # Execute
            result = await call_get_time_buckets(albumId=sample_uuid)

            # Assert
            assert len(result) == 1
            assert result[0].timeBucket == "2024-01-01"
            assert result[0].count == 1
            mock_client.albums.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_time_buckets_with_person_id(self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid):
        """Test time buckets with person filter."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets
            assets = multiple_gumnut_assets
            assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
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
    async def test_get_time_buckets_ascending_order(self, multiple_gumnut_assets, mock_sync_cursor_page):
        """Test time buckets with ascending order."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets with different dates
            assets = multiple_gumnut_assets
            assets[0].local_datetime = datetime(2024, 2, 15, 10, 0, 0, tzinfo=timezone.utc)
            assets[0].created_at = assets[0].local_datetime
            assets[1].local_datetime = datetime(2024, 1, 20, 14, 0, 0, tzinfo=timezone.utc)
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
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
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
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
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
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
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
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
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
    async def test_get_time_bucket_success(self, multiple_gumnut_assets, mock_sync_cursor_page):
        """Test successful retrieval of time bucket assets."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets - some in January 2024, some in February
            assets = multiple_gumnut_assets
            # Asset 0: January 2024 (should be included)
            assets[0].id = "asset-jan-1"
            assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
            assets[0].created_at = assets[0].local_datetime
            assets[0].mime_type = "image/jpeg"
            assets[0].aspect_ratio = 1.5
            assets[0].local_datetime_offset = -5

            # Asset 1: February 2024 (should NOT be included)
            assets[1].id = "asset-feb-1"
            assets[1].local_datetime = datetime(2024, 2, 20, 14, 0, 0, tzinfo=timezone.utc)
            assets[1].created_at = assets[1].local_datetime
            assets[1].mime_type = "video/mp4"
            assets[1].aspect_ratio = 1.78
            assets[1].local_datetime_offset = 0

            # Asset 2: January 2024 (should be included)
            assets[2].id = "asset-jan-2"
            assets[2].local_datetime = datetime(2024, 1, 25, 16, 0, 0, tzinfo=timezone.utc)
            assets[2].created_at = assets[2].local_datetime
            assets[2].mime_type = "image/png"
            assets[2].aspect_ratio = 1.0
            assets[2].local_datetime_offset = 2

            mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

            # Mock get_current_user_id
            with patch('routers.api.timeline.get_current_user_id') as mock_user_id:
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

                # Check aspect ratios
                assert result["ratio"][0] == 1.5
                assert result["ratio"][1] == 1.0

                # Check timezone offsets
                assert result["localOffsetHours"][0] == -5
                assert result["localOffsetHours"][1] == 2

                # Check fixed fields
                assert all(fav is False for fav in result["isFavorite"])
                assert all(trash is False for trash in result["isTrashed"])
                assert all(vis == AssetVisibility.timeline for vis in result["visibility"])

    @pytest.mark.anyio
    async def test_get_time_bucket_with_album_id(self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid):
        """Test time bucket with album filter."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets
            assets = multiple_gumnut_assets
            assets[0].id = "asset-1"
            assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
            assets[0].created_at = assets[0].local_datetime
            assets[0].mime_type = "image/jpeg"
            assets[0].aspect_ratio = 1.0
            assets[0].local_datetime_offset = 0

            mock_client.albums.assets.list.return_value = mock_sync_cursor_page([assets[0]])

            # Mock get_current_user_id
            with patch('routers.api.timeline.get_current_user_id') as mock_user_id:
                mock_user_id.return_value = sample_uuid

                # Execute
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00", albumId=sample_uuid)

                # Assert
                assert len(result["id"]) == 1
                mock_client.albums.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_time_bucket_with_person_id(self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid):
        """Test time bucket with person filter."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets
            assets = multiple_gumnut_assets
            assets[0].id = "asset-1"
            assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
            assets[0].created_at = assets[0].local_datetime
            assets[0].mime_type = "image/jpeg"
            assets[0].aspect_ratio = 1.0
            assets[0].local_datetime_offset = 0

            mock_client.assets.list.return_value = mock_sync_cursor_page([assets[0]])

            # Mock get_current_user_id
            with patch('routers.api.timeline.get_current_user_id') as mock_user_id:
                mock_user_id.return_value = sample_uuid

                # Execute
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00", personId=sample_uuid)

                # Assert
                assert len(result["id"]) == 1
                # Should be called with person_id parameter
                mock_client.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_time_bucket_string_datetime(self, mock_sync_cursor_page):
        """Test handling of string datetime in assets."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Create mock asset with string datetime
            mock_asset = Mock()
            mock_asset.id = "asset-1"
            mock_asset.local_datetime = "2024-01-15T10:00:00Z"
            mock_asset.created_at = "2024-01-15T10:00:00Z"
            mock_asset.mime_type = "image/jpeg"
            mock_asset.aspect_ratio = 1.0
            mock_asset.local_datetime_offset = 0

            mock_client.assets.list.return_value = mock_sync_cursor_page([mock_asset])

            # Mock get_current_user_id
            with patch('routers.api.timeline.get_current_user_id') as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert
                assert len(result["id"]) == 1
                # Should have parsed the string datetime correctly
                assert "2024-01-15T10:00:00" in result["fileCreatedAt"][0]

    @pytest.mark.anyio
    async def test_get_time_bucket_no_matching_assets(self, multiple_gumnut_assets, mock_sync_cursor_page):
        """Test time bucket with no assets matching the time."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup test assets from February 2024
            assets = multiple_gumnut_assets
            assets[0].local_datetime = datetime(2024, 2, 15, 10, 0, 0, tzinfo=timezone.utc)
            assets[0].created_at = assets[0].local_datetime

            mock_client.assets.list.return_value = mock_sync_cursor_page(assets[:1])

            # Mock get_current_user_id
            with patch('routers.api.timeline.get_current_user_id') as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute - request January 2024 bucket (no matching assets)
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert
                assert len(result["id"]) == 0
                assert len(result["fileCreatedAt"]) == 0
                assert len(result["isImage"]) == 0

    @pytest.mark.anyio
    async def test_get_time_bucket_dict_format_assets(self, mock_sync_cursor_page):
        """Test handling of dict-format assets (as opposed to object format)."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Create dict-format asset
            dict_asset = {
                "id": "dict-asset-1",
                "local_datetime": "2024-01-15T10:00:00Z",
                "created_at": "2024-01-15T10:00:00Z",
                "mime_type": "image/jpeg",
                "aspect_ratio": 1.5,
                "local_datetime_offset": -3
            }

            mock_client.assets.list.return_value = mock_sync_cursor_page([dict_asset])

            # Mock get_current_user_id
            with patch('routers.api.timeline.get_current_user_id') as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert
                assert len(result["id"]) == 1
                assert result["ratio"][0] == 1.5
                assert result["localOffsetHours"][0] == -3
                assert result["isImage"][0] is True

    @pytest.mark.anyio
    async def test_get_time_bucket_missing_attributes(self, mock_sync_cursor_page):
        """Test handling of assets with missing attributes."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Create mock asset with minimal attributes
            mock_asset = Mock()
            mock_asset.id = "minimal-asset"
            mock_asset.local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
            mock_asset.created_at = None
            # Set defaults for missing attributes to avoid Mock conversion errors
            mock_asset.mime_type = ""
            mock_asset.aspect_ratio = 1.0
            mock_asset.local_datetime_offset = 0

            mock_client.assets.list.return_value = mock_sync_cursor_page([mock_asset])

            # Mock get_current_user_id
            with patch('routers.api.timeline.get_current_user_id') as mock_user_id:
                mock_user_id.return_value = uuid4()

                # Execute
                result = await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

                # Assert
                assert len(result["id"]) == 1
                # Should use defaults for missing attributes
                assert result["ratio"][0] == 1.0  # Default aspect ratio
                assert result["localOffsetHours"][0] == 0  # Default offset
                assert result["isImage"][0] is True  # Default to image when no mime_type

    @pytest.mark.anyio
    async def test_get_time_bucket_invalid_date_format(self):
        """Test handling of invalid timeBucket format."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
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
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
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
        with patch('routers.api.timeline.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.side_effect = Exception("401 Invalid API key")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await call_get_time_bucket(timeBucket="2024-01-01T00:00:00")

            assert exc_info.value.status_code == 401