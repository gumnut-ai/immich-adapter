"""Tests for assets.py endpoints."""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock, patch
from fastapi import HTTPException
from uuid import uuid4
import base64

from gumnut import GumnutError
from socketio.exceptions import SocketIOError

from services.websockets import WebSocketEvent

from routers.api.assets import (
    _immich_checksum_to_base64,
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
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id
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
    async def test_bulk_upload_check_all_new(self):
        """Test bulk upload check when all assets are new."""
        # Valid SHA-1 checksums (40 hex characters)
        checksum1 = "a" * 40
        checksum2 = "b" * 40

        request = AssetBulkUploadCheckDto(
            assets=[
                AssetBulkUploadCheckItem(id="asset1", checksum=checksum1),
                AssetBulkUploadCheckItem(id="asset2", checksum=checksum2),
            ]
        )

        # Mock the Gumnut client - no existing assets
        mock_client = Mock()
        mock_response = Mock()
        mock_response.assets = []
        mock_client.assets.check_existence.return_value = mock_response

        # Execute
        result = await bulk_upload_check(request, client=mock_client)

        # Assert
        assert len(result.results) == 2
        assert all(item.action == Action.accept for item in result.results)
        assert result.results[0].id == "asset1"
        assert result.results[1].id == "asset2"
        mock_client.assets.check_existence.assert_called_once()

    @pytest.mark.anyio
    async def test_bulk_upload_check_with_duplicates(self, sample_uuid):
        """Test bulk upload check when some assets already exist."""
        # Valid SHA-1 checksums (40 hex characters)
        checksum1 = "a" * 40
        checksum2 = "b" * 40

        request = AssetBulkUploadCheckDto(
            assets=[
                AssetBulkUploadCheckItem(id="asset1", checksum=checksum1),
                AssetBulkUploadCheckItem(id="asset2", checksum=checksum2),
            ]
        )

        # Mock the Gumnut client - first asset exists
        mock_client = Mock()
        mock_existing_asset = Mock()
        mock_existing_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        # The checksum is stored as base64 in Gumnut
        mock_existing_asset.checksum_sha1 = base64.b64encode(
            bytes.fromhex(checksum1)
        ).decode("ascii")

        mock_response = Mock()
        mock_response.assets = [mock_existing_asset]
        mock_client.assets.check_existence.return_value = mock_response

        # Execute
        result = await bulk_upload_check(request, client=mock_client)

        # Assert
        assert len(result.results) == 2
        # First asset should be rejected as duplicate
        assert result.results[0].id == "asset1"
        assert result.results[0].action == Action.reject
        assert result.results[0].assetId == str(sample_uuid)
        # Second asset should be accepted
        assert result.results[1].id == "asset2"
        assert result.results[1].action == Action.accept

    @pytest.mark.anyio
    async def test_bulk_upload_check_with_base64_checksum(self, sample_uuid):
        """Test bulk upload check with base64-encoded checksums (mobile client format)."""
        # SHA-1 is 20 bytes, which encodes to 28 base64 characters
        # Create a valid 20-byte value and encode it
        sha1_bytes = b"\xaa" * 20  # 20 bytes of 0xaa
        checksum_b64 = base64.b64encode(sha1_bytes).decode("ascii")  # 28 chars
        assert len(checksum_b64) == 28  # Verify it's the expected length

        request = AssetBulkUploadCheckDto(
            assets=[
                AssetBulkUploadCheckItem(id="mobile-asset-1", checksum=checksum_b64),
            ]
        )

        # Mock the Gumnut client - asset exists with matching base64 checksum
        mock_client = Mock()
        mock_existing_asset = Mock()
        mock_existing_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        # Gumnut stores checksums as base64, should match exactly
        mock_existing_asset.checksum_sha1 = checksum_b64

        mock_response = Mock()
        mock_response.assets = [mock_existing_asset]
        mock_client.assets.check_existence.return_value = mock_response

        # Execute
        result = await bulk_upload_check(request, client=mock_client)

        # Assert - should detect as duplicate
        assert len(result.results) == 1
        assert result.results[0].id == "mobile-asset-1"
        assert result.results[0].action == Action.reject
        assert result.results[0].assetId == str(sample_uuid)

        # Verify the base64 checksum was passed to Gumnut as-is
        mock_client.assets.check_existence.assert_called_once()
        call_args = mock_client.assets.check_existence.call_args
        assert checksum_b64 in call_args.kwargs["checksum_sha1s"]

    @pytest.mark.anyio
    async def test_bulk_upload_check_with_malformed_checksum(self):
        """Test bulk upload check with malformed hex checksum.

        Matches Immich server behavior: invalid checksums produce empty buffers
        silently, causing duplicate detection to fail (false negative) rather
        than throwing an error.
        """
        # Mix of valid and invalid checksums
        valid_checksum = "a" * 40  # Valid hex
        invalid_checksum = "invalidhex!!!"  # Invalid hex characters

        request = AssetBulkUploadCheckDto(
            assets=[
                AssetBulkUploadCheckItem(id="valid-asset", checksum=valid_checksum),
                AssetBulkUploadCheckItem(id="invalid-asset", checksum=invalid_checksum),
            ]
        )

        # Mock the Gumnut client - no existing assets
        mock_client = Mock()
        mock_response = Mock()
        mock_response.assets = []
        mock_client.assets.check_existence.return_value = mock_response

        # Execute - should NOT raise an exception
        result = await bulk_upload_check(request, client=mock_client)

        # Assert - both assets should be accepted (invalid one has false negative)
        assert len(result.results) == 2
        assert all(item.action == Action.accept for item in result.results)


class TestImmichChecksumToBase64:
    """Test the _immich_checksum_to_base64 helper function.

    These tests verify that the function matches Immich server behavior:
    - Valid hex checksums are converted to base64
    - Valid base64 checksums (28 chars) are passed through unchanged
    - Invalid hex checksums produce empty base64 strings (silent failure)
    """

    def test_valid_hex_checksum(self):
        """Test conversion of valid 40-character hex checksum."""
        hex_checksum = "aabbccdd11223344556677889900aabbccddeeff"
        result = _immich_checksum_to_base64(hex_checksum)

        # Verify it's valid base64 that decodes to the original bytes
        decoded = base64.b64decode(result)
        assert decoded == bytes.fromhex(hex_checksum)

    def test_valid_base64_checksum(self):
        """Test that 28-character base64 checksums are passed through unchanged."""
        # SHA-1 is 20 bytes, which encodes to 28 base64 characters
        sha1_bytes = b"\xaa" * 20
        checksum_b64 = base64.b64encode(sha1_bytes).decode("ascii")
        assert len(checksum_b64) == 28

        result = _immich_checksum_to_base64(checksum_b64)
        assert result == checksum_b64

    def test_invalid_hex_characters(self):
        """Test that invalid hex characters produce empty base64 (matches Immich)."""
        result = _immich_checksum_to_base64("invalidhex!!!")

        # Should return empty base64 (base64 encoding of empty bytes)
        assert result == ""
        assert base64.b64decode(result) == b""

    def test_mixed_valid_invalid_hex(self):
        """Test hex string with valid prefix but invalid characters."""
        result = _immich_checksum_to_base64("aabb!!ccdd")

        # Should return empty base64 (matches Immich's silent failure)
        assert result == ""

    def test_odd_length_hex(self):
        """Test odd-length hex string (invalid)."""
        result = _immich_checksum_to_base64("aabbccdde")  # 9 chars, odd

        # Should return empty base64
        assert result == ""

    def test_empty_string(self):
        """Test empty string input."""
        result = _immich_checksum_to_base64("")

        # Empty hex produces empty base64
        assert result == ""

    def test_short_valid_hex(self):
        """Test short but valid hex string."""
        result = _immich_checksum_to_base64("aabbccdd")

        # Should convert successfully
        decoded = base64.b64decode(result)
        assert decoded == bytes.fromhex("aabbccdd")


class TestCheckExistingAssets:
    """Test the check_existing_assets endpoint."""

    @pytest.mark.anyio
    async def test_check_existing_assets_returns_empty(self):
        """Test that check_existing_assets returns empty list when no assets exist."""
        # Setup
        request = CheckExistingAssetsDto(
            deviceAssetIds=["asset1", "asset2"], deviceId="device-123"
        )

        # Mock the Gumnut client - no existing assets
        mock_client = Mock()
        mock_response = Mock()
        mock_response.assets = []
        mock_client.assets.check_existence.return_value = mock_response

        # Execute
        result = await check_existing_assets(request, client=mock_client)

        # Assert
        assert result.existingIds == []
        mock_client.assets.check_existence.assert_called_once_with(
            device_id="device-123", device_asset_ids=["asset1", "asset2"]
        )

    @pytest.mark.anyio
    async def test_check_existing_assets_returns_existing(self, sample_uuid):
        """Test that check_existing_assets returns IDs of existing assets."""
        # Setup
        request = CheckExistingAssetsDto(
            deviceAssetIds=["asset1", "asset2", "asset3"], deviceId="device-123"
        )

        # Mock the Gumnut client - some assets exist
        mock_client = Mock()
        mock_asset1 = Mock()
        mock_asset1.id = uuid_to_gumnut_asset_id(sample_uuid)
        second_uuid = uuid4()
        mock_asset2 = Mock()
        mock_asset2.id = uuid_to_gumnut_asset_id(second_uuid)

        mock_response = Mock()
        mock_response.assets = [mock_asset1, mock_asset2]
        mock_client.assets.check_existence.return_value = mock_response

        # Execute
        result = await check_existing_assets(request, client=mock_client)

        # Assert
        assert len(result.existingIds) == 2
        assert str(sample_uuid) in result.existingIds
        assert str(second_uuid) in result.existingIds


class TestUploadAsset:
    """Test the upload_asset endpoint."""

    @pytest.mark.anyio
    async def test_upload_asset_success(self, sample_uuid, mock_current_user):
        """Test successful asset upload."""
        # Setup - create mock client
        mock_client = Mock()

        # Mock the gumnut asset response with proper Gumnut ID format
        mock_gumnut_asset = Mock()
        mock_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_gumnut_asset.checksum = "abc123"
        mock_gumnut_asset.original_file_name = "test.jpg"
        mock_gumnut_asset.created_at = datetime.now(timezone.utc)
        mock_gumnut_asset.updated_at = datetime.now(timezone.utc)
        mock_gumnut_asset.mime_type = "image/jpeg"
        mock_gumnut_asset.width = 1920
        mock_gumnut_asset.height = 1080
        mock_gumnut_asset.file_size_bytes = 1024
        mock_gumnut_asset.exif = None
        mock_gumnut_asset.people = []
        mock_client.assets.create.return_value = mock_gumnut_asset

        # Mock the file data
        mock_file = Mock()
        mock_file.filename = "test.jpg"
        mock_file.content_type = "image/jpeg"
        mock_file.read = AsyncMock(return_value=b"fake image data")

        # Execute
        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await upload_asset(
                assetData=mock_file,
                deviceAssetId="device-123",
                deviceId="device-456",
                fileCreatedAt="2023-01-01T12:00:00Z",
                fileModifiedAt="2023-01-01T12:00:00Z",
                isFavorite=False,
                duration="",
                client=mock_client,
                current_user=mock_current_user,
            )

        # Assert
        assert result.id == str(sample_uuid)
        assert result.status == AssetMediaStatus.created
        mock_client.assets.create.assert_called_once()

    @pytest.mark.anyio
    async def test_upload_asset_duplicate(self, sample_uuid, mock_current_user):
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
        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await upload_asset(
                assetData=mock_file,
                deviceAssetId="device-123",
                deviceId="device-456",
                fileCreatedAt="2023-01-01T12:00:00Z",
                client=mock_client,
                current_user=mock_current_user,
            )

        # Assert
        assert result.status == AssetMediaStatus.duplicate
        assert result.id == "00000000-0000-0000-0000-000000000000"

    @pytest.mark.anyio
    async def test_upload_asset_api_error(self, mock_current_user):
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
            with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
                await upload_asset(
                    assetData=mock_file,
                    deviceAssetId="device-123",
                    deviceId="device-456",
                    fileCreatedAt="2023-01-01T12:00:00Z",
                    client=mock_client,
                    current_user=mock_current_user,
                )

        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_upload_asset_emits_websocket_events(
        self, sample_uuid, mock_current_user
    ):
        """Test that upload_asset emits on_upload_success and AssetUploadReadyV1 events."""
        # Setup - create mock client
        mock_client = Mock()

        # Mock the gumnut asset response
        mock_gumnut_asset = Mock()
        mock_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_gumnut_asset.checksum = "abc123"
        mock_gumnut_asset.original_file_name = "test.jpg"
        mock_gumnut_asset.created_at = datetime.now(timezone.utc)
        mock_gumnut_asset.updated_at = datetime.now(timezone.utc)
        mock_gumnut_asset.mime_type = "image/jpeg"
        mock_gumnut_asset.width = 1920
        mock_gumnut_asset.height = 1080
        mock_gumnut_asset.file_size_bytes = 1024
        mock_gumnut_asset.exif = None
        mock_gumnut_asset.people = []
        mock_client.assets.create.return_value = mock_gumnut_asset

        # Mock the file data
        mock_file = Mock()
        mock_file.filename = "test.jpg"
        mock_file.content_type = "image/jpeg"
        mock_file.read = AsyncMock(return_value=b"fake image data")

        # Execute with mocked emit_event
        with patch(
            "routers.api.assets.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            await upload_asset(
                assetData=mock_file,
                deviceAssetId="device-123",
                deviceId="device-456",
                fileCreatedAt="2023-01-01T12:00:00Z",
                client=mock_client,
                current_user=mock_current_user,
            )

            # Assert - emit_event should be called twice
            assert mock_emit.call_count == 2

            # First call should be on_upload_success
            first_call = mock_emit.call_args_list[0]
            assert first_call[0][0] == WebSocketEvent.UPLOAD_SUCCESS
            assert first_call[0][1] == mock_current_user.id

            # Second call should be AssetUploadReadyV1
            second_call = mock_emit.call_args_list[1]
            assert second_call[0][0] == WebSocketEvent.ASSET_UPLOAD_READY_V1
            assert second_call[0][1] == mock_current_user.id

    @pytest.mark.anyio
    async def test_upload_asset_websocket_error_does_not_fail_upload(
        self, sample_uuid, mock_current_user
    ):
        """Test that WebSocket emission errors don't fail the upload."""
        # Setup - create mock client
        mock_client = Mock()

        # Mock the gumnut asset response
        mock_gumnut_asset = Mock()
        mock_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_gumnut_asset.checksum = "abc123"
        mock_gumnut_asset.original_file_name = "test.jpg"
        mock_gumnut_asset.created_at = datetime.now(timezone.utc)
        mock_gumnut_asset.updated_at = datetime.now(timezone.utc)
        mock_gumnut_asset.mime_type = "image/jpeg"
        mock_gumnut_asset.width = 1920
        mock_gumnut_asset.height = 1080
        mock_gumnut_asset.file_size_bytes = 1024
        mock_gumnut_asset.exif = None
        mock_gumnut_asset.people = []
        mock_client.assets.create.return_value = mock_gumnut_asset

        # Mock the file data
        mock_file = Mock()
        mock_file.filename = "test.jpg"
        mock_file.content_type = "image/jpeg"
        mock_file.read = AsyncMock(return_value=b"fake image data")

        # Execute with emit_event that raises a SocketIOError
        with patch(
            "routers.api.assets.emit_user_event",
            new_callable=AsyncMock,
            side_effect=SocketIOError("WebSocket error"),
        ):
            result = await upload_asset(
                assetData=mock_file,
                deviceAssetId="device-123",
                deviceId="device-456",
                fileCreatedAt="2023-01-01T12:00:00Z",
                client=mock_client,
                current_user=mock_current_user,
            )

            # Upload should still succeed despite WebSocket error
            assert result.id == str(sample_uuid)
            assert result.status == AssetMediaStatus.created


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
        current_user_id = uuid4()

        # Execute
        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

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
            GumnutError("404 Not found"),  # Failure
        ]

        asset_ids = [uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)
        current_user_id = uuid4()

        # Execute
        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        # Assert - should still return 204 even with partial failures
        assert result.status_code == 204
        assert mock_client.assets.delete.call_count == 2

    @pytest.mark.anyio
    async def test_delete_assets_emits_websocket_events(self):
        """Test that delete_assets emits on_asset_delete for each deleted asset."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.delete.return_value = None

        asset_ids = [uuid4(), uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)
        current_user_id = uuid4()

        # Execute
        with patch(
            "routers.api.assets.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

            # Assert - emit_event should be called for each deleted asset
            assert mock_emit.call_count == 3

            # Verify each call has correct event type and user ID
            for i, call in enumerate(mock_emit.call_args_list):
                assert call[0][0] == WebSocketEvent.ASSET_DELETE
                assert call[0][1] == str(current_user_id)
                assert call[0][2] == str(asset_ids[i])

    @pytest.mark.anyio
    async def test_delete_assets_websocket_error_does_not_fail_deletion(self):
        """Test that WebSocket emission errors don't fail the deletion."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.delete.return_value = None

        asset_ids = [uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)
        current_user_id = uuid4()

        # Execute with emit_event that raises a SocketIOError
        with patch(
            "routers.api.assets.emit_user_event",
            new_callable=AsyncMock,
            side_effect=SocketIOError("WebSocket error"),
        ):
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

            # Deletion should still succeed despite WebSocket error
            assert result.status_code == 204


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
    async def test_update_asset_success(
        self, sample_gumnut_asset, sample_uuid, mock_current_user
    ):
        """Test successful asset update (calls get_asset_info)."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.retrieve.return_value = sample_gumnut_asset

        request = UpdateAssetDto(isFavorite=True)

        # Execute
        result = await update_asset(
            sample_uuid, request, client=mock_client, current_user=mock_current_user
        )

        # Assert
        # Should return a converted AssetResponseDto from get_asset_info
        assert hasattr(result, "id")
        assert hasattr(result, "deviceAssetId")
        mock_client.assets.retrieve.assert_called_once()


class TestGetAssetInfo:
    """Test the get_asset_info endpoint."""

    @pytest.mark.anyio
    async def test_get_asset_info_success(
        self, sample_gumnut_asset, sample_uuid, mock_current_user
    ):
        """Test successful retrieval of asset info."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.retrieve.return_value = sample_gumnut_asset

        # Execute
        result = await get_asset_info(
            sample_uuid, client=mock_client, current_user=mock_current_user
        )

        # Assert
        # Result should be a real AssetResponseDto from conversion
        assert hasattr(result, "id")
        assert hasattr(result, "deviceAssetId")
        mock_client.assets.retrieve.assert_called_once()

    @pytest.mark.anyio
    async def test_get_asset_info_not_found(self, sample_uuid, mock_current_user):
        """Test handling of asset not found."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.retrieve.side_effect = Exception("404 Not found")

        # Execute & Assert
        with pytest.raises(HTTPException) as exc_info:
            await get_asset_info(
                sample_uuid, client=mock_client, current_user=mock_current_user
            )

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
    async def test_view_asset_fullsize_uses_thumbnail_api(self, sample_uuid):
        """Test that /thumbnail?size=fullsize uses download_thumbnail, not download.

        This is critical for HEIC support: Immich requests fullsize thumbnails
        expecting browser-compatible formats (WEBP), not the original HEIC.
        See GUM-223 for details.
        """
        # Setup - create mock client
        mock_client = Mock()

        # Mock the streaming response context manager
        mock_response = Mock()
        mock_response.headers = {"content-type": "image/webp"}
        mock_response.iter_bytes.return_value = iter([b"fake image data"])

        mock_context = Mock()
        mock_context.__enter__ = Mock(return_value=mock_response)
        mock_context.__exit__ = Mock(return_value=None)

        # Mock download_thumbnail, NOT download
        mock_client.assets.with_streaming_response.download_thumbnail.return_value = (
            mock_context
        )

        # Execute
        result = await view_asset(
            sample_uuid, size=AssetMediaSize.fullsize, client=mock_client
        )

        # Assert
        assert result.media_type == "image/webp"
        assert hasattr(result, "body_iterator")  # StreamingResponse has body_iterator
        # Verify download_thumbnail was called with size="fullsize", NOT download()
        # Called twice: once for headers, once for streaming
        assert (
            mock_client.assets.with_streaming_response.download_thumbnail.call_count
            == 2
        )
        mock_client.assets.with_streaming_response.download.assert_not_called()

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

    @pytest.mark.anyio
    async def test_download_asset_uses_download_not_thumbnail(self, sample_uuid):
        """Test that /original endpoint uses download(), not download_thumbnail().

        The /original endpoint should always return the actual original file
        (JPEG, HEIC, RAW, etc.), not a converted thumbnail. This preserves the
        original format for downloads.
        See GUM-223 for details on the distinction from /thumbnail endpoint.
        """
        # Setup - create mock client
        mock_client = Mock()

        # Mock the streaming response context manager
        mock_response = Mock()
        mock_response.headers = {
            "content-type": "image/heic",
            "content-disposition": 'attachment; filename="IMG_1234.heic"',
        }
        mock_response.iter_bytes.return_value = iter([b"fake heic data"])

        mock_context = Mock()
        mock_context.__enter__ = Mock(return_value=mock_response)
        mock_context.__exit__ = Mock(return_value=None)

        mock_client.assets.with_streaming_response.download.return_value = mock_context

        # Execute
        result = await download_asset(sample_uuid, client=mock_client)

        # Assert - download() was called, NOT download_thumbnail()
        assert result.media_type == "image/heic"
        # Called twice: once for headers, once for streaming
        assert mock_client.assets.with_streaming_response.download.call_count == 2
        mock_client.assets.with_streaming_response.download_thumbnail.assert_not_called()


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
        """Test video playback (stub implementation)."""
        # Execute
        result = await play_asset_video(sample_uuid)

        # Assert
        assert result.status_code == 200
