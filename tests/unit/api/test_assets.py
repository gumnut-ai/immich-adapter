"""Tests for assets.py endpoints."""

import json

import pytest
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import Mock, AsyncMock, patch
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from uuid import UUID, uuid4
import base64

from socketio.exceptions import SocketIOError

from services.websockets import WebSocketEvent

from routers.api.assets import (
    _extract_upload_fields,
    _immich_checksum_to_base64,
    _parse_datetime,
    bulk_upload_check,
    check_existing_assets,
    copy_asset,
    upload_asset,
    update_assets,
    delete_assets,
    get_all_user_assets_by_device_id,
    get_asset_statistics,
    get_random,
    run_asset_jobs,
    update_asset,
    get_asset_info,
    get_asset_ocr,
    view_asset,
    download_asset,
    replace_asset,
    get_asset_metadata,
    update_asset_metadata,
    delete_asset_metadata,
    get_asset_metadata_by_key,
)
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id
from routers.immich_models import (
    Action,
    AssetBulkUploadCheckDto,
    AssetBulkUploadCheckItem,
    AssetCopyDto,
    AssetMediaResponseDto,
    CheckExistingAssetsDto,
    AssetBulkUpdateDto,
    AssetBulkDeleteDto,
    AssetJobsDto,
    AssetJobName,
    UpdateAssetDto,
    AssetMetadataUpsertDto,
    AssetMetadataUpsertItemDto,
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
        mock_client.assets.check_existence = AsyncMock(return_value=mock_response)

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
        mock_client.assets.check_existence = AsyncMock(return_value=mock_response)

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
        mock_client.assets.check_existence = AsyncMock(return_value=mock_response)

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
        mock_client.assets.check_existence = AsyncMock(return_value=mock_response)

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
        mock_client.assets.check_existence = AsyncMock(return_value=mock_response)

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
        mock_client.assets.check_existence = AsyncMock(return_value=mock_response)

        # Execute
        result = await check_existing_assets(request, client=mock_client)

        # Assert
        assert len(result.existingIds) == 2
        assert str(sample_uuid) in result.existingIds
        assert str(second_uuid) in result.existingIds


def _make_mock_request(
    content_length: int = 1024,
    form_data: dict | None = None,
    mock_file: Mock | None = None,
) -> Mock:
    """Create a mock Request for upload_asset tests.

    The request has a small content-length (below default threshold) to trigger
    the buffered path, with form() returning the given form data + file.
    """
    request = Mock()
    request.headers = {
        "content-length": str(content_length),
        "content-type": "multipart/form-data; boundary=---abc123",
    }

    class _State:
        jwt_token = "test-jwt-token"

    request.state = _State()

    # Build form dict from form_data + file
    if form_data is None:
        form_data = {
            "deviceAssetId": "device-123",
            "deviceId": "device-456",
            "fileCreatedAt": "2023-01-01T12:00:00Z",
            "fileModifiedAt": "2023-01-01T12:00:00Z",
        }
    if mock_file is None:
        mock_file = Mock()
        mock_file.filename = "test.jpg"
        mock_file.content_type = "image/jpeg"
        mock_file.file = BytesIO(b"fake image data")
        mock_file.seek = AsyncMock()

    merged = {**form_data, "assetData": mock_file}

    # request.form() is an async context manager
    form_ctx = AsyncMock()
    form_ctx.__aenter__ = AsyncMock(return_value=merged)
    form_ctx.__aexit__ = AsyncMock(return_value=False)
    request.form = Mock(return_value=form_ctx)

    return request


def _make_mock_settings(threshold: int = 200 * 1024 * 1024) -> Mock:
    """Create a mock Settings with a given streaming threshold."""
    settings = Mock()
    settings.streaming_upload_threshold_bytes = threshold
    settings.gumnut_api_base_url = "http://localhost:8000"
    return settings


class TestUploadAsset:
    """Test the upload_asset endpoint."""

    @pytest.mark.anyio
    async def test_upload_asset_success(self, sample_uuid, mock_current_user):
        """Test successful asset upload via buffered path."""
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
        mock_gumnut_asset.metadata = None
        mock_gumnut_asset.people = []

        mock_raw_response = Mock()
        mock_raw_response.status_code = 201
        mock_raw_response.parse = AsyncMock(return_value=mock_gumnut_asset)

        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock(
            return_value=mock_raw_response
        )

        mock_file = Mock()
        mock_file.filename = "test.jpg"
        mock_file.content_type = "image/jpeg"
        mock_file.file = BytesIO(b"fake image data")
        mock_file.seek = AsyncMock()

        request = _make_mock_request(mock_file=mock_file)
        settings = _make_mock_settings()

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await upload_asset(
                request=request,
                client=mock_client,
                current_user=mock_current_user,
                settings=settings,
            )

        assert isinstance(result, AssetMediaResponseDto)
        assert result.id == str(sample_uuid)
        assert result.status == AssetMediaStatus.created
        mock_client.assets.with_raw_response.create.assert_called_once()
        call_kwargs = mock_client.assets.with_raw_response.create.call_args
        assert call_kwargs.kwargs["asset_data"][1] is mock_file.file

    @pytest.mark.anyio
    async def test_upload_asset_duplicate_returns_real_id(
        self, sample_uuid, mock_current_user
    ):
        """Test that duplicate upload (HTTP 200 from photos-api) returns the real asset ID."""
        mock_gumnut_asset = Mock()
        mock_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)

        mock_raw_response = Mock()
        mock_raw_response.status_code = 200
        mock_raw_response.parse = AsyncMock(return_value=mock_gumnut_asset)

        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock(
            return_value=mock_raw_response
        )

        request = _make_mock_request()
        settings = _make_mock_settings()

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await upload_asset(
                request=request,
                client=mock_client,
                current_user=mock_current_user,
                settings=settings,
            )

        assert isinstance(result, JSONResponse)
        assert result.status_code == 200
        assert json.loads(bytes(result.body)) == {
            "id": str(sample_uuid),
            "status": "duplicate",
        }

    @pytest.mark.anyio
    async def test_upload_asset_api_error(self, mock_current_user):
        """An auth error during upload is mapped to 401 via map_gumnut_error."""
        from gumnut import AuthenticationError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock(
            side_effect=make_sdk_status_error(
                401, "Invalid API key", cls=AuthenticationError
            )
        )

        request = _make_mock_request()
        settings = _make_mock_settings()

        with pytest.raises(HTTPException) as exc_info:
            with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
                await upload_asset(
                    request=request,
                    client=mock_client,
                    current_user=mock_current_user,
                    settings=settings,
                )

        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_upload_asset_emits_websocket_events(
        self, sample_uuid, mock_current_user
    ):
        """Test that upload_asset emits on_upload_success and AssetUploadReadyV1 events."""
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
        mock_gumnut_asset.metadata = None
        mock_gumnut_asset.people = []

        mock_raw_response = Mock()
        mock_raw_response.status_code = 201
        mock_raw_response.parse = AsyncMock(return_value=mock_gumnut_asset)

        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock(
            return_value=mock_raw_response
        )

        request = _make_mock_request()
        settings = _make_mock_settings()

        with patch(
            "routers.api.assets.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            await upload_asset(
                request=request,
                client=mock_client,
                current_user=mock_current_user,
                settings=settings,
            )

            assert mock_emit.call_count == 2
            first_call = mock_emit.call_args_list[0]
            assert first_call[0][0] == WebSocketEvent.UPLOAD_SUCCESS
            assert first_call[0][1] == mock_current_user.id

            second_call = mock_emit.call_args_list[1]
            assert second_call[0][0] == WebSocketEvent.ASSET_UPLOAD_READY_V1
            assert second_call[0][1] == mock_current_user.id

    @pytest.mark.anyio
    async def test_upload_live_photo_mov_is_dropped(self, mock_current_user):
        """Test that iOS live photo .MOV files are silently dropped."""
        mock_client = Mock()

        mock_file = Mock()
        mock_file.filename = "IMG_1234.MOV"
        mock_file.content_type = "application/octet-stream"
        mock_file.file = BytesIO(b"fake live photo data")
        mock_file.seek = AsyncMock()

        request = _make_mock_request(mock_file=mock_file)
        settings = _make_mock_settings()

        with patch("routers.api.assets.is_live_photo_video", return_value=True):
            result = await upload_asset(
                request=request,
                client=mock_client,
                current_user=mock_current_user,
                settings=settings,
            )

        assert isinstance(result, AssetMediaResponseDto)
        assert UUID(result.id)
        assert result.status == AssetMediaStatus.created
        mock_client.assets.with_raw_response.create.assert_not_called()

    @pytest.mark.anyio
    async def test_upload_live_photo_mov_with_video_content_type_is_dropped(
        self, mock_current_user
    ):
        """Test that live photo .MOV with video/* content type is also dropped."""
        mock_client = Mock()

        mock_file = Mock()
        mock_file.filename = "IMG_1234.MOV"
        mock_file.content_type = "video/quicktime"
        mock_file.file = BytesIO(b"fake live photo data")
        mock_file.seek = AsyncMock()

        request = _make_mock_request(mock_file=mock_file)
        settings = _make_mock_settings()

        with patch("routers.api.assets.is_live_photo_video", return_value=True):
            result = await upload_asset(
                request=request,
                client=mock_client,
                current_user=mock_current_user,
                settings=settings,
            )

        assert isinstance(result, AssetMediaResponseDto)
        assert UUID(result.id)
        assert result.status == AssetMediaStatus.created
        mock_client.assets.with_raw_response.create.assert_not_called()

    @pytest.mark.anyio
    async def test_upload_regular_video_proceeds(self, sample_uuid, mock_current_user):
        """Test that regular video uploads are not dropped."""
        mock_gumnut_asset = Mock()
        mock_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_gumnut_asset.checksum = "abc123"
        mock_gumnut_asset.original_file_name = "video.mp4"
        mock_gumnut_asset.created_at = datetime.now(timezone.utc)
        mock_gumnut_asset.updated_at = datetime.now(timezone.utc)
        mock_gumnut_asset.mime_type = "video/mp4"
        mock_gumnut_asset.width = 1920
        mock_gumnut_asset.height = 1080
        mock_gumnut_asset.file_size_bytes = 10240
        mock_gumnut_asset.metadata = None
        mock_gumnut_asset.people = []

        mock_raw_response = Mock()
        mock_raw_response.status_code = 201
        mock_raw_response.parse = AsyncMock(return_value=mock_gumnut_asset)

        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock(
            return_value=mock_raw_response
        )

        mock_file = Mock()
        mock_file.filename = "video.mp4"
        mock_file.content_type = "video/mp4"
        mock_file.file = BytesIO(b"fake video data")
        mock_file.seek = AsyncMock()

        request = _make_mock_request(mock_file=mock_file)
        settings = _make_mock_settings()

        with (
            patch("routers.api.assets.is_live_photo_video", return_value=False),
            patch("routers.api.assets.emit_user_event", new_callable=AsyncMock),
        ):
            result = await upload_asset(
                request=request,
                client=mock_client,
                current_user=mock_current_user,
                settings=settings,
            )

        assert isinstance(result, AssetMediaResponseDto)
        assert result.id == str(sample_uuid)
        assert result.status == AssetMediaStatus.created
        mock_client.assets.with_raw_response.create.assert_called_once()

    @pytest.mark.anyio
    async def test_upload_asset_websocket_error_does_not_fail_upload(
        self, sample_uuid, mock_current_user
    ):
        """Test that WebSocket emission errors don't fail the upload."""
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
        mock_gumnut_asset.metadata = None
        mock_gumnut_asset.people = []

        mock_raw_response = Mock()
        mock_raw_response.status_code = 201
        mock_raw_response.parse = AsyncMock(return_value=mock_gumnut_asset)

        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock(
            return_value=mock_raw_response
        )

        request = _make_mock_request()
        settings = _make_mock_settings()

        with patch(
            "routers.api.assets.emit_user_event",
            new_callable=AsyncMock,
            side_effect=SocketIOError("WebSocket error"),
        ):
            result = await upload_asset(
                request=request,
                client=mock_client,
                current_user=mock_current_user,
                settings=settings,
            )

            assert isinstance(result, AssetMediaResponseDto)
            assert result.id == str(sample_uuid)
            assert result.status == AssetMediaStatus.created

    @pytest.mark.anyio
    async def test_upload_strategy_selection_buffered(self, mock_current_user):
        """Test that small content-length selects buffered strategy."""
        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock(
            side_effect=Exception("test error")
        )

        # Content-Length 1024 < threshold 100MB → buffered
        request = _make_mock_request(content_length=1024)
        settings = _make_mock_settings(threshold=100 * 1024 * 1024)

        with pytest.raises(HTTPException):
            with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
                await upload_asset(
                    request=request,
                    client=mock_client,
                    current_user=mock_current_user,
                    settings=settings,
                )

        # Buffered path was used (form() was called)
        request.form.assert_called_once()

    @pytest.mark.anyio
    async def test_upload_strategy_selection_streaming(self, mock_current_user):
        """Test that large content-length selects streaming strategy."""
        request = Mock()
        request.headers = {
            "content-length": str(200 * 1024 * 1024),  # 200MB > threshold
            "content-type": "multipart/form-data; boundary=---abc123",
        }

        class _State:
            jwt_token = "test-jwt-token"

        request.state = _State()

        settings = _make_mock_settings(threshold=100 * 1024 * 1024)

        # Streaming path calls request.stream(), not request.form()
        with patch(
            "routers.api.assets._upload_streaming", new_callable=AsyncMock
        ) as mock_streaming:
            mock_streaming.return_value = AssetMediaResponseDto(
                id=str(uuid4()), status=AssetMediaStatus.created
            )
            await upload_asset(
                request=request,
                client=Mock(),
                current_user=mock_current_user,
                settings=settings,
            )

        mock_streaming.assert_called_once()

    @pytest.mark.anyio
    async def test_upload_strategy_threshold_boundary_uses_buffered(
        self, mock_current_user
    ):
        """Content-Length exactly at threshold uses buffered (strict > comparison)."""
        threshold = 100 * 1024 * 1024
        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock(
            side_effect=Exception("test error")
        )

        request = _make_mock_request(content_length=threshold)
        settings = _make_mock_settings(threshold=threshold)

        with pytest.raises(HTTPException):
            with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
                await upload_asset(
                    request=request,
                    client=mock_client,
                    current_user=mock_current_user,
                    settings=settings,
                )

        # At boundary → buffered path (form() called)
        request.form.assert_called_once()

    @pytest.mark.anyio
    async def test_upload_strategy_missing_content_length_uses_buffered(
        self, mock_current_user
    ):
        """Missing Content-Length header falls through to buffered path."""
        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock(
            side_effect=Exception("test error")
        )

        request = _make_mock_request(content_length=1024)
        # Remove content-length header to simulate missing
        del request.headers["content-length"]
        settings = _make_mock_settings(threshold=100 * 1024 * 1024)

        with pytest.raises(HTTPException):
            with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
                await upload_asset(
                    request=request,
                    client=mock_client,
                    current_user=mock_current_user,
                    settings=settings,
                )

        request.form.assert_called_once()

    @pytest.mark.anyio
    async def test_upload_strategy_invalid_content_length_uses_buffered(
        self, mock_current_user
    ):
        """Non-numeric Content-Length falls through to buffered path."""
        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock(
            side_effect=Exception("test error")
        )

        request = _make_mock_request(content_length=1024)
        request.headers["content-length"] = "not-a-number"
        settings = _make_mock_settings(threshold=100 * 1024 * 1024)

        with pytest.raises(HTTPException):
            with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
                await upload_asset(
                    request=request,
                    client=mock_client,
                    current_user=mock_current_user,
                    settings=settings,
                )

        request.form.assert_called_once()

    @pytest.mark.anyio
    async def test_upload_strategy_threshold_zero_forces_streaming(
        self, mock_current_user
    ):
        """threshold=0 forces all uploads to streaming regardless of size."""
        request = Mock()
        request.headers = {
            "content-length": "1024",  # Small file
            "content-type": "multipart/form-data; boundary=---abc123",
        }

        class _State:
            jwt_token = "test-jwt-token"

        request.state = _State()
        settings = _make_mock_settings(threshold=0)

        with patch(
            "routers.api.assets._upload_streaming", new_callable=AsyncMock
        ) as mock_streaming:
            mock_streaming.return_value = AssetMediaResponseDto(
                id=str(uuid4()), status=AssetMediaStatus.created
            )
            await upload_asset(
                request=request,
                client=Mock(),
                current_user=mock_current_user,
                settings=settings,
            )

        mock_streaming.assert_called_once()

    @pytest.mark.anyio
    async def test_streaming_upload_duplicate_returns_real_id(
        self, sample_uuid, mock_current_user
    ):
        """Test that streaming path returns real asset ID for duplicates (HTTP 200)."""
        gumnut_id = uuid_to_gumnut_asset_id(sample_uuid)

        request = Mock()
        request.headers = {
            "content-length": str(300 * 1024 * 1024),
            "content-type": "multipart/form-data; boundary=---abc123",
        }

        class _State:
            jwt_token = "test-jwt-token"

        request.state = _State()

        settings = _make_mock_settings(threshold=100 * 1024 * 1024)

        mock_pipeline_instance = Mock()
        mock_pipeline_instance.execute = AsyncMock(return_value={"id": gumnut_id})
        mock_pipeline_instance.last_status_code = 200

        with patch(
            "routers.api.assets.StreamingUploadPipeline",
            return_value=mock_pipeline_instance,
        ):
            result = await upload_asset(
                request=request,
                client=Mock(),
                current_user=mock_current_user,
                settings=settings,
            )

        assert isinstance(result, JSONResponse)
        assert result.status_code == 200
        assert json.loads(bytes(result.body)) == {
            "id": str(sample_uuid),
            "status": "duplicate",
        }

    @pytest.mark.anyio
    async def test_streaming_upload_duplicate_missing_id_raises(
        self, mock_current_user
    ):
        """Test that streaming duplicate with no asset ID raises 502."""
        request = Mock()
        request.headers = {
            "content-length": str(300 * 1024 * 1024),
            "content-type": "multipart/form-data; boundary=---abc123",
        }

        class _State:
            jwt_token = "test-jwt-token"

        request.state = _State()

        settings = _make_mock_settings(threshold=100 * 1024 * 1024)

        mock_pipeline_instance = Mock()
        mock_pipeline_instance.execute = AsyncMock(return_value={})
        mock_pipeline_instance.last_status_code = 200

        with patch(
            "routers.api.assets.StreamingUploadPipeline",
            return_value=mock_pipeline_instance,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await upload_asset(
                    request=request,
                    client=Mock(),
                    current_user=mock_current_user,
                    settings=settings,
                )

        assert exc_info.value.status_code == 502

    @pytest.mark.anyio
    async def test_streaming_upload_missing_status_code_raises(self, mock_current_user):
        """Test that missing pipeline.last_status_code raises 502."""
        request = Mock()
        request.headers = {
            "content-length": str(300 * 1024 * 1024),
            "content-type": "multipart/form-data; boundary=---abc123",
        }

        class _State:
            jwt_token = "test-jwt-token"

        request.state = _State()

        settings = _make_mock_settings(threshold=100 * 1024 * 1024)

        mock_pipeline_instance = Mock()
        mock_pipeline_instance.execute = AsyncMock(return_value={"id": "asset_123"})
        mock_pipeline_instance.last_status_code = None

        with patch(
            "routers.api.assets.StreamingUploadPipeline",
            return_value=mock_pipeline_instance,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await upload_asset(
                    request=request,
                    client=Mock(),
                    current_user=mock_current_user,
                    settings=settings,
                )

        assert exc_info.value.status_code == 502


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
        mock_client.assets.delete = AsyncMock(return_value=None)

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
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()

        # First delete succeeds, second fails with NotFoundError (already gone).
        mock_client.assets.delete = AsyncMock(
            side_effect=[
                None,
                make_sdk_status_error(404, "Not found", cls=NotFoundError),
            ]
        )

        asset_ids = [uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)
        current_user_id = uuid4()

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        # Per-item errors are logged and skipped; bulk endpoint still returns 204.
        assert result.status_code == 204
        assert mock_client.assets.delete.call_count == 2

    @pytest.mark.anyio
    async def test_delete_assets_non_404_does_not_abort_batch(self):
        """A 5xx upstream error on one item must not abort the batch."""
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.delete = AsyncMock(
            side_effect=[None, make_sdk_status_error(500, "boom")]
        )

        asset_ids = [uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)
        current_user_id = uuid4()

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert result.status_code == 204
        assert mock_client.assets.delete.call_count == 2

    @pytest.mark.anyio
    async def test_delete_assets_connection_error_does_not_abort_batch(self):
        """A transport error on one item must not abort the batch."""
        from tests.conftest import make_sdk_connection_error

        mock_client = Mock()
        mock_client.assets.delete = AsyncMock(
            side_effect=[None, make_sdk_connection_error("DELETE")]
        )

        asset_ids = [uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)
        current_user_id = uuid4()

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert result.status_code == 204
        assert mock_client.assets.delete.call_count == 2

    @pytest.mark.anyio
    async def test_delete_assets_emits_websocket_events(self):
        """Test that delete_assets emits on_asset_delete for each deleted asset."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.assets.delete = AsyncMock(return_value=None)

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
        mock_client.assets.delete = AsyncMock(return_value=None)

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
    async def test_get_asset_statistics_propagates_sdk_error(self):
        """SDK errors bubble up to the global GumnutError handler."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.list.side_effect = make_sdk_status_error(500, "boom")

        with pytest.raises(APIStatusError):
            await get_asset_statistics(client=mock_client)


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
        mock_client.assets.retrieve = AsyncMock(return_value=sample_gumnut_asset)

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
        mock_client.assets.retrieve = AsyncMock(return_value=sample_gumnut_asset)

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
    async def test_get_asset_info_not_found_propagates(
        self, sample_uuid, mock_current_user
    ):
        """A NotFoundError on retrieve bubbles up to the global handler."""
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            side_effect=make_sdk_status_error(404, "Not found", cls=NotFoundError)
        )

        with pytest.raises(NotFoundError):
            await get_asset_info(
                sample_uuid, client=mock_client, current_user=mock_current_user
            )


def _make_mock_asset_with_urls(variant_map: dict[str, dict[str, str]]):
    """Create a mock asset with asset_urls (Mock objects with .url/.mimetype attrs)."""
    asset = Mock()
    mock_urls = {}
    for key, val in variant_map.items():
        variant = Mock()
        variant.url = val["url"]
        variant.mimetype = val["mimetype"]
        mock_urls[key] = variant
    asset.asset_urls = mock_urls
    return asset


class TestViewAsset:
    """Test the view_asset endpoint."""

    @pytest.mark.anyio
    async def test_view_asset_success(self, sample_uuid):
        """Test successful asset thumbnail view via CDN."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "thumbnail": {
                        "url": "https://cdn.example.com/thumb.webp",
                        "mimetype": "image/webp",
                    }
                }
            )
        )
        mock_streaming_response = Mock()

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = mock_streaming_response
            result = await view_asset(
                sample_uuid, size=AssetMediaSize.thumbnail, client=mock_client
            )

        assert result is mock_streaming_response
        mock_client.assets.retrieve.assert_called_once()
        mock_cdn.assert_called_once_with(
            "https://cdn.example.com/thumb.webp",
            "image/webp",
            range_header=None,
            forwarded_headers=(
                "content-length",
                "etag",
                "last-modified",
                "cache-control",
            ),
        )

    @pytest.mark.anyio
    async def test_view_asset_fullsize_maps_to_fullsize_variant(self, sample_uuid):
        """Test that ?size=fullsize maps to the 'fullsize' asset_urls variant."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "fullsize": {
                        "url": "https://cdn.example.com/full.webp",
                        "mimetype": "image/webp",
                    }
                }
            )
        )

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = Mock()
            await view_asset(
                sample_uuid, size=AssetMediaSize.fullsize, client=mock_client
            )

        mock_cdn.assert_called_once_with(
            "https://cdn.example.com/full.webp",
            "image/webp",
            range_header=None,
            forwarded_headers=(
                "content-length",
                "etag",
                "last-modified",
                "cache-control",
            ),
        )

    @pytest.mark.anyio
    async def test_view_asset_not_found_propagates(self, sample_uuid):
        """A NotFoundError on retrieve bubbles up to the global handler."""
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            side_effect=make_sdk_status_error(404, "Not found", cls=NotFoundError)
        )

        with pytest.raises(NotFoundError):
            await view_asset(sample_uuid, client=mock_client)

    @pytest.mark.anyio
    async def test_view_asset_missing_variant(self, sample_uuid):
        """Test 404 when requested variant is not in asset_urls."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "original": {
                        "url": "https://cdn.example.com/orig.jpg",
                        "mimetype": "image/jpeg",
                    }
                }
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            await view_asset(
                sample_uuid, size=AssetMediaSize.thumbnail, client=mock_client
            )

        assert exc_info.value.status_code == 404


class TestDownloadAsset:
    """Test the download_asset endpoint."""

    @pytest.mark.anyio
    async def test_download_asset_success(self, sample_uuid):
        """Test successful asset download via CDN original variant."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "original": {
                        "url": "https://cdn.example.com/original.jpg",
                        "mimetype": "image/jpeg",
                    }
                }
            )
        )
        mock_streaming_response = Mock()

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = mock_streaming_response
            result = await download_asset(sample_uuid, client=mock_client)

        assert result is mock_streaming_response
        mock_client.assets.retrieve.assert_called_once()
        mock_cdn.assert_called_once_with(
            "https://cdn.example.com/original.jpg",
            "image/jpeg",
            range_header=None,
            forwarded_headers=(
                "content-length",
                "etag",
                "last-modified",
                "cache-control",
                "content-disposition",
            ),
        )

    @pytest.mark.anyio
    async def test_download_asset_heic_original(self, sample_uuid):
        """Test that /original returns HEIC format (not converted)."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "original": {
                        "url": "https://cdn.example.com/IMG_1234.heic",
                        "mimetype": "image/heic",
                    }
                }
            )
        )

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = Mock()
            await download_asset(sample_uuid, client=mock_client)

        mock_cdn.assert_called_once_with(
            "https://cdn.example.com/IMG_1234.heic",
            "image/heic",
            range_header=None,
            forwarded_headers=(
                "content-length",
                "etag",
                "last-modified",
                "cache-control",
                "content-disposition",
            ),
        )


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
                AssetMetadataUpsertItemDto(key="mobile_app", value={"test": "value"})
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
        result = await delete_asset_metadata(sample_uuid, "mobile_app")

        # Assert
        assert result is None


class TestGetAssetMetadataByKey:
    """Test the get_asset_metadata_by_key endpoint."""

    @pytest.mark.anyio
    async def test_get_asset_metadata_by_key_returns_none(self, sample_uuid):
        """Test that get_asset_metadata_by_key returns None."""
        # Execute
        result = await get_asset_metadata_by_key(sample_uuid, "mobile_app")

        # Assert
        assert result is None


class TestCopyAsset:
    """Test the copy_asset endpoint."""

    @pytest.mark.anyio
    async def test_copy_asset_returns_204(self, sample_uuid):
        """Test that copy_asset returns 204 No Content."""
        request = AssetCopyDto(sourceId=sample_uuid, targetId=uuid4())

        result = await copy_asset(request)

        assert result.status_code == 204


class TestGetAssetOcr:
    """Test the get_asset_ocr endpoint."""

    @pytest.mark.anyio
    async def test_get_asset_ocr_returns_empty_list(self, sample_uuid):
        """Test that get_asset_ocr returns an empty list."""
        result = await get_asset_ocr(sample_uuid)

        assert result == []


class TestParseDateTime:
    """Tests for _parse_datetime helper."""

    def test_valid_iso_with_z_suffix(self):
        fallback = datetime(2000, 1, 1, tzinfo=timezone.utc)
        result = _parse_datetime("2023-06-15T10:30:00Z", fallback)
        assert result.year == 2023
        assert result.month == 6
        assert result.tzinfo is not None

    def test_naive_datetime_gets_fallback_tz(self):
        fallback = datetime(2000, 1, 1, tzinfo=timezone.utc)
        result = _parse_datetime("2023-06-15T10:30:00", fallback)
        assert result.year == 2023
        assert result.tzinfo == timezone.utc

    def test_invalid_string_returns_fallback(self):
        fallback = datetime(2000, 1, 1, tzinfo=timezone.utc)
        assert _parse_datetime("not-a-date", fallback) == fallback

    def test_empty_string_returns_fallback(self):
        fallback = datetime(2000, 1, 1, tzinfo=timezone.utc)
        assert _parse_datetime("", fallback) == fallback

    def test_none_returns_fallback(self):
        fallback = datetime(2000, 1, 1, tzinfo=timezone.utc)
        assert _parse_datetime(None, fallback) == fallback


class TestExtractUploadFields:
    """Tests for _extract_upload_fields helper."""

    def test_valid_fields(self):
        fields = {
            "deviceAssetId": "device-123",
            "deviceId": "device-456",
            "fileCreatedAt": "2023-06-15T10:30:00Z",
            "fileModifiedAt": "2023-06-15T11:00:00Z",
        }
        result = _extract_upload_fields(fields)
        assert result.device_asset_id == "device-123"
        assert result.device_id == "device-456"
        assert result.file_created_at.year == 2023

    def test_missing_required_field_raises(self):
        with pytest.raises(ValueError, match="Missing required"):
            _extract_upload_fields({"deviceAssetId": "x", "deviceId": "y"})

    def test_file_modified_at_defaults_to_created(self):
        fields = {
            "deviceAssetId": "x",
            "deviceId": "y",
            "fileCreatedAt": "2023-06-15T10:30:00Z",
        }
        result = _extract_upload_fields(fields)
        assert result.file_modified_at == result.file_created_at
