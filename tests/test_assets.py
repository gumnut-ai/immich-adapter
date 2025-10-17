"""Tests for assets.py endpoints."""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from fastapi import HTTPException
from uuid import uuid4

from routers.api.assets import (
    bulk_upload_check,
    check_existing_assets,
    upload_asset,
    update_assets,
    delete_assets,
    get_all_user_assets_by_device_id,
    get_asset_statistics,
    get_random,
    run_asset_jobs,
    update_asset,
    get_asset_info,
    view_asset,
    download_asset,
    replace_asset,
    get_asset_metadata,
    update_asset_metadata,
    delete_asset_metadata,
    get_asset_metadata_by_key,
    play_asset_video,
)
from routers.immich_models import (
    Action,
    AssetBulkUploadCheckDto,
    AssetBulkUploadCheckItem,
    CheckExistingAssetsDto,
    AssetBulkUpdateDto,
    AssetBulkDeleteDto,
    AssetJobsDto,
    AssetJobName,
    UpdateAssetDto,
    AssetMetadataUpsertDto,
    AssetMetadataUpsertItemDto,
    AssetMetadataKey,
    AssetMediaSize,
    AssetMediaStatus,
)


class TestBulkUploadCheck:
    """Test the bulk_upload_check endpoint."""

    @pytest.mark.anyio
    async def test_bulk_upload_check_success(self):
        """Test successful bulk upload check."""
        request = AssetBulkUploadCheckDto(
            assets=[
                AssetBulkUploadCheckItem(id="asset1", checksum="checksum1"),
                AssetBulkUploadCheckItem(id="asset2", checksum="checksum2"),
            ]
        )

        # Execute
        result = await bulk_upload_check(request)

        # Assert
        # NOTE: Pydantic converts the dictionaries from assets.py to AssetBulkUploadCheckResult objects
        assert len(result.results) == 2
        assert all(item.action == Action.accept for item in result.results)
        assert result.results[0].id == "asset1"
        assert result.results[1].id == "asset2"


class TestCheckExistingAssets:
    """Test the check_existing_assets endpoint."""

    @pytest.mark.anyio
    async def test_check_existing_assets_returns_empty(self):
        """Test that check_existing_assets returns empty list."""
        # Setup
        request = CheckExistingAssetsDto(
            deviceAssetIds=["asset1", "asset2"], deviceId="device-123"
        )

        # Execute
        result = await check_existing_assets(request)

        # Assert
        assert result.existingIds == []


class TestUploadAsset:
    """Test the upload_asset endpoint."""

    @pytest.mark.anyio
    async def test_upload_asset_success(self, sample_uuid):
        """Test successful asset upload."""
        # Setup - create mock client
        mock_client = Mock()

        # Mock the gumnut asset response
        mock_gumnut_asset = Mock()
        mock_gumnut_asset.id = "gumnut-asset-123"
        mock_client.assets.create.return_value = mock_gumnut_asset

        # Mock the file data
        mock_file = Mock()
        mock_file.filename = "test.jpg"
        mock_file.content_type = "image/jpeg"
        mock_file.read = AsyncMock(return_value=b"fake image data")

        # Mock safe_uuid_from_asset_id
        with patch(
            "routers.utils.gumnut_id_conversion.safe_uuid_from_asset_id"
        ) as mock_safe_uuid:
            mock_safe_uuid.return_value = sample_uuid

            # Execute
            result = await upload_asset(
                assetData=mock_file,
                deviceAssetId="device-123",
                deviceId="device-456",
                fileCreatedAt="2023-01-01T12:00:00Z",
                fileModifiedAt="2023-01-01T12:00:00Z",
                isFavorite=False,
                duration="",
                client=mock_client,
            )

            # Assert
            assert result.id == str(sample_uuid)
            assert result.status == AssetMediaStatus.created
            mock_client.assets.create.assert_called_once()

    @pytest.mark.anyio
    async def test_upload_asset_duplicate(self, sample_uuid):
        """Test upload asset with duplicate error."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.create.side_effect = Exception("Asset already exists")

        # Mock the file data
        mock_file = Mock()
        mock_file.filename = "test.jpg"
        mock_file.content_type = "image/jpeg"
        mock_file.read = AsyncMock(return_value=b"fake image data")

        # Execute
        result = await upload_asset(
            assetData=mock_file,
            deviceAssetId="device-123",
            deviceId="device-456",
            fileCreatedAt="2023-01-01T12:00:00Z",
            client=mock_client,
        )

        # Assert
        assert result.status == AssetMediaStatus.duplicate
        assert result.id == "00000000-0000-0000-0000-000000000000"

    @pytest.mark.anyio
    async def test_upload_asset_api_error(self):
        """Test upload asset with API error."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.create.side_effect = Exception("401 Invalid API key")

        # Mock the file data
        mock_file = Mock()
        mock_file.filename = "test.jpg"
        mock_file.content_type = "image/jpeg"
        mock_file.read = AsyncMock(return_value=b"fake image data")

        # Execute & Assert
        with pytest.raises(HTTPException) as exc_info:
            await upload_asset(
                assetData=mock_file,
                deviceAssetId="device-123",
                deviceId="device-456",
                fileCreatedAt="2023-01-01T12:00:00Z",
                client=mock_client,
            )

        assert exc_info.value.status_code == 401


class TestUpdateAssets:
    """Test the update_assets endpoint."""

    @pytest.mark.anyio
    async def test_update_assets_success(self):
        """Test successful assets update (stub implementation)."""
        # Setup
        request = AssetBulkUpdateDto(
            ids=[uuid4()], dateTimeOriginal="2023-01-01T12:00:00Z"
        )

        # Execute
        result = await update_assets(request)

        # Assert
        assert result.status_code == 204


class TestDeleteAssets:
    """Test the delete_assets endpoint."""

    @pytest.mark.anyio
    async def test_delete_assets_success(self):
        """Test successful assets deletion."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.delete.return_value = None

        asset_ids = [uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)

        # Execute
        result = await delete_assets(request, client=mock_client)

        # Assert
        assert result.status_code == 204
        assert mock_client.assets.delete.call_count == 2

    @pytest.mark.anyio
    async def test_delete_assets_partial_failure(self):
        """Test deletion with some assets not found."""
        # Setup - create mock client
        mock_client = Mock()

        # First delete succeeds, second fails with 404
        mock_client.assets.delete.side_effect = [
            None,  # Success
            Exception("404 Not found"),  # Failure
        ]

        asset_ids = [uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)

        # Execute
        result = await delete_assets(request, client=mock_client)

        # Assert - should still return 204 even with partial failures
        assert result.status_code == 204
        assert mock_client.assets.delete.call_count == 2


class TestGetAllUserAssetsByDeviceId:
    """Test the get_all_user_assets_by_device_id endpoint."""

    @pytest.mark.anyio
    async def test_get_all_user_assets_by_device_id_returns_empty(self):
        """Test that get_all_user_assets_by_device_id returns empty list."""
        # Execute
        result = await get_all_user_assets_by_device_id("device-123")

        # Assert
        assert result == []


class TestGetAssetStatistics:
    """Test the get_asset_statistics endpoint."""

    @pytest.mark.anyio
    async def test_get_asset_statistics_success(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """Test successful retrieval of asset statistics."""
        # Setup - create mock client
        mock_client = Mock()

        # Modify assets to have different mime types
        assets = multiple_gumnut_assets
        assets[0].mime_type = "image/jpeg"
        assets[1].mime_type = "video/mp4"
        assets[2].mime_type = "image/png"

        mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

        # Execute
        result = await get_asset_statistics(client=mock_client)

        # Assert
        assert result.total == 3
        assert result.images == 2  # Two image assets
        assert result.videos == 1  # One video asset
        mock_client.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_asset_statistics_empty(self, mock_sync_cursor_page):
        """Test asset statistics with no assets."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        # Execute
        result = await get_asset_statistics(client=mock_client)

        # Assert
        assert result.total == 0
        assert result.images == 0
        assert result.videos == 0

    @pytest.mark.anyio
    async def test_get_asset_statistics_gumnut_error(self):
        """Test handling of Gumnut API errors."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.list.side_effect = Exception("API Error")

        # Execute & Assert
        with pytest.raises(HTTPException) as exc_info:
            await get_asset_statistics(client=mock_client)

        assert exc_info.value.status_code == 500


class TestGetRandom:
    """Test the get_random endpoint."""

    @pytest.mark.anyio
    async def test_get_random_returns_empty(self):
        """Test that get_random returns empty list (deprecated endpoint)."""
        # Execute
        result = await get_random(count=5)

        # Assert
        assert result == []


class TestRunAssetJobs:
    """Test the run_asset_jobs endpoint."""

    @pytest.mark.anyio
    async def test_run_asset_jobs_success(self):
        """Test successful asset jobs run (stub implementation)."""
        # Setup
        request = AssetJobsDto(
            assetIds=[uuid4()], name=AssetJobName.regenerate_thumbnail
        )

        # Execute
        result = await run_asset_jobs(request)

        # Assert
        assert result.status_code == 204


class TestUpdateAsset:
    """Test the update_asset endpoint."""

    @pytest.mark.anyio
    async def test_update_asset_success(self, sample_gumnut_asset, sample_uuid):
        """Test successful asset update (calls get_asset_info)."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.retrieve.return_value = sample_gumnut_asset

        request = UpdateAssetDto(isFavorite=True)

        # Execute
        result = await update_asset(sample_uuid, request, client=mock_client)

        # Assert
        # Should return a converted AssetResponseDto from get_asset_info
        assert hasattr(result, "id")
        assert hasattr(result, "deviceAssetId")
        mock_client.assets.retrieve.assert_called_once()


class TestGetAssetInfo:
    """Test the get_asset_info endpoint."""

    @pytest.mark.anyio
    async def test_get_asset_info_success(self, sample_gumnut_asset, sample_uuid):
        """Test successful retrieval of asset info."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.retrieve.return_value = sample_gumnut_asset

        # Execute
        result = await get_asset_info(sample_uuid, client=mock_client)

        # Assert
        # Result should be a real AssetResponseDto from conversion
        assert hasattr(result, "id")
        assert hasattr(result, "deviceAssetId")
        mock_client.assets.retrieve.assert_called_once()

    @pytest.mark.anyio
    async def test_get_asset_info_not_found(self, sample_uuid):
        """Test handling of asset not found."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.retrieve.side_effect = Exception("404 Not found")

        # Execute & Assert
        with pytest.raises(HTTPException) as exc_info:
            await get_asset_info(sample_uuid, client=mock_client)

        assert exc_info.value.status_code == 404


class TestViewAsset:
    """Test the view_asset endpoint."""

    @pytest.mark.anyio
    async def test_view_asset_success(self, sample_uuid):
        """Test successful asset thumbnail view."""
        # Setup - create mock client
        mock_client = Mock()

        # Mock the streaming response context manager
        mock_response = Mock()
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_response.iter_bytes.return_value = iter([b"fake image data"])

        mock_context = Mock()
        mock_context.__enter__ = Mock(return_value=mock_response)
        mock_context.__exit__ = Mock(return_value=None)

        mock_client.assets.with_streaming_response.download_thumbnail.return_value = (
            mock_context
        )

        # Execute
        result = await view_asset(
            sample_uuid, size=AssetMediaSize.thumbnail, client=mock_client
        )

        # Assert
        assert result.media_type == "image/jpeg"
        assert hasattr(result, "body_iterator")  # StreamingResponse has body_iterator
        # Called twice: once for headers, once for streaming
        assert (
            mock_client.assets.with_streaming_response.download_thumbnail.call_count
            == 2
        )

    @pytest.mark.anyio
    async def test_view_asset_fullsize(self, sample_uuid):
        """Test asset view with fullsize."""
        # Setup - create mock client
        mock_client = Mock()

        # Mock the streaming response context manager
        mock_response = Mock()
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_response.iter_bytes.return_value = iter([b"fake image data"])

        mock_context = Mock()
        mock_context.__enter__ = Mock(return_value=mock_response)
        mock_context.__exit__ = Mock(return_value=None)

        mock_client.assets.with_streaming_response.download.return_value = mock_context

        # Execute
        result = await view_asset(
            sample_uuid, size=AssetMediaSize.fullsize, client=mock_client
        )

        # Assert
        assert result.media_type == "image/jpeg"
        assert hasattr(result, "body_iterator")  # StreamingResponse has body_iterator
        # Called twice: once for headers, once for streaming
        assert mock_client.assets.with_streaming_response.download.call_count == 2

    @pytest.mark.anyio
    async def test_view_asset_not_found(self, sample_uuid):
        """Test handling of asset not found during view."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.with_streaming_response.download_thumbnail.side_effect = (
            Exception("404 Not found")
        )

        # Execute & Assert
        with pytest.raises(HTTPException) as exc_info:
            await view_asset(sample_uuid, client=mock_client)

        assert exc_info.value.status_code == 404


class TestDownloadAsset:
    """Test the download_asset endpoint."""

    @pytest.mark.anyio
    async def test_download_asset_success(self, sample_uuid):
        """Test successful asset download."""
        # Setup - create mock client
        mock_client = Mock()

        # Mock the streaming response context manager
        mock_response = Mock()
        mock_response.headers = {
            "content-type": "image/jpeg",
            "content-disposition": 'attachment; filename="test.jpg"',
        }
        mock_response.iter_bytes.return_value = iter([b"fake image data"])

        mock_context = Mock()
        mock_context.__enter__ = Mock(return_value=mock_response)
        mock_context.__exit__ = Mock(return_value=None)

        mock_client.assets.with_streaming_response.download.return_value = mock_context

        # Execute
        result = await download_asset(sample_uuid, client=mock_client)

        # Assert
        assert result.media_type == "image/jpeg"
        assert hasattr(result, "body_iterator")  # StreamingResponse has body_iterator
        assert "Content-Disposition" in result.headers
        # Called twice: once for headers, once for streaming
        assert mock_client.assets.with_streaming_response.download.call_count == 2


class TestReplaceAsset:
    """Test the replace_asset endpoint."""

    @pytest.mark.anyio
    async def test_replace_asset_returns_none(self, sample_uuid):
        """Test replace asset (deprecated, returns None)."""
        # Setup
        request = Mock()  # AssetMediaReplaceDto mock

        # Execute
        result = await replace_asset(sample_uuid, request)

        # Assert
        assert result is None


class TestGetAssetMetadata:
    """Test the get_asset_metadata endpoint."""

    @pytest.mark.anyio
    async def test_get_asset_metadata_returns_empty(self, sample_uuid):
        """Test that get_asset_metadata returns empty list."""
        # Execute
        result = await get_asset_metadata(sample_uuid)

        # Assert
        assert result == []


class TestUpdateAssetMetadata:
    """Test the update_asset_metadata endpoint."""

    @pytest.mark.anyio
    async def test_update_asset_metadata_returns_empty(self, sample_uuid):
        """Test that update_asset_metadata returns empty list."""
        # Setup
        request = AssetMetadataUpsertDto(
            items=[
                AssetMetadataUpsertItemDto(
                    key=AssetMetadataKey.mobile_app, value={"test": "value"}
                )
            ]
        )

        # Execute
        result = await update_asset_metadata(sample_uuid, request)

        # Assert
        assert result == []


class TestDeleteAssetMetadata:
    """Test the delete_asset_metadata endpoint."""

    @pytest.mark.anyio
    async def test_delete_asset_metadata_returns_none(self, sample_uuid):
        """Test delete asset metadata (stub implementation)."""
        # Execute
        result = await delete_asset_metadata(sample_uuid, AssetMetadataKey.mobile_app)

        # Assert
        assert result is None


class TestGetAssetMetadataByKey:
    """Test the get_asset_metadata_by_key endpoint."""

    @pytest.mark.anyio
    async def test_get_asset_metadata_by_key_returns_none(self, sample_uuid):
        """Test that get_asset_metadata_by_key returns None."""
        # Execute
        result = await get_asset_metadata_by_key(
            sample_uuid, AssetMetadataKey.mobile_app
        )

        # Assert
        assert result is None


class TestPlayAssetVideo:
    """Test the play_asset_video endpoint."""

    @pytest.mark.anyio
    async def test_play_asset_video_success(self, sample_uuid):
        """Test video playback."""
        # Setup - create mock client
        mock_client = Mock()

        # Mock the streaming response context manager
        mock_response = Mock()
        mock_response.headers = {"content-type": "video/mp4"}
        mock_response.iter_bytes.return_value = iter([b"fake video data"])

        mock_context = Mock()
        mock_context.__enter__ = Mock(return_value=mock_response)
        mock_context.__exit__ = Mock(return_value=None)

        mock_client.assets.with_streaming_response.download.return_value = mock_context

        # Execute
        result = await play_asset_video(sample_uuid, client=mock_client)

        # Assert
        assert result.media_type == "video/mp4"
        assert hasattr(result, "body_iterator")
        # Called twice: once for headers, once for streaming
        assert mock_client.assets.with_streaming_response.download.call_count == 2
