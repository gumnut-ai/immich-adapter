"""Tests for assets.py endpoints."""

import asyncio
import json

import pytest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any
from unittest.mock import Mock, AsyncMock, patch
from zoneinfo import ZoneInfo
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
    play_asset_video,
)
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id
from routers.immich_models import (
    Action,
    AssetBulkUploadCheckDto,
    AssetBulkUploadCheckItem,
    AssetCopyDto,
    AssetMediaResponseDto,
    AssetVisibility,
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
        mock_gumnut_asset.checksum_sha1 = "PaDX6+c+Lhjpm5/ciXUROL1ryaU="
        mock_gumnut_asset.thumbhash = None
        mock_gumnut_asset.original_file_name = "test.jpg"
        mock_gumnut_asset.created_at = datetime.now(timezone.utc)
        mock_gumnut_asset.updated_at = datetime.now(timezone.utc)
        mock_gumnut_asset.mime_type = "image/jpeg"
        mock_gumnut_asset.width = 1920
        mock_gumnut_asset.height = 1080
        mock_gumnut_asset.duration = None
        mock_gumnut_asset.file_size_bytes = 1024
        mock_gumnut_asset.metadata = None
        mock_gumnut_asset.people = []
        mock_gumnut_asset.trashed_at = None

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
    async def test_upload_asset_buffered_client_disconnect(self, mock_current_user):
        """A mid-upload client disconnect on the buffered path returns 499 instead
        of escaping as an unhandled 500."""
        from starlette.requests import ClientDisconnect

        mock_client = Mock()
        mock_client.assets.with_raw_response.create = AsyncMock()

        request = _make_mock_request()
        # Starlette raises ClientDisconnect from request.form() when the client
        # hangs up while the multipart body is still being parsed.
        form_ctx = AsyncMock()
        form_ctx.__aenter__ = AsyncMock(side_effect=ClientDisconnect())
        request.form = Mock(return_value=form_ctx)
        settings = _make_mock_settings()

        result = await upload_asset(
            request=request,
            client=mock_client,
            current_user=mock_current_user,
            settings=settings,
        )

        assert isinstance(result, JSONResponse)
        assert result.status_code == 499
        mock_client.assets.with_raw_response.create.assert_not_called()

    @pytest.mark.anyio
    async def test_upload_asset_emits_websocket_events(
        self, sample_uuid, mock_current_user
    ):
        """Test that upload_asset emits on_upload_success and AssetUploadReadyV1 events."""
        mock_gumnut_asset = Mock()
        mock_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_gumnut_asset.checksum = "abc123"
        mock_gumnut_asset.checksum_sha1 = "PaDX6+c+Lhjpm5/ciXUROL1ryaU="
        mock_gumnut_asset.thumbhash = None
        mock_gumnut_asset.original_file_name = "test.jpg"
        mock_gumnut_asset.created_at = datetime.now(timezone.utc)
        mock_gumnut_asset.updated_at = datetime.now(timezone.utc)
        mock_gumnut_asset.local_datetime = mock_gumnut_asset.created_at
        mock_gumnut_asset.file_created_at = mock_gumnut_asset.created_at
        mock_gumnut_asset.file_modified_at = mock_gumnut_asset.updated_at
        mock_gumnut_asset.mime_type = "image/jpeg"
        mock_gumnut_asset.width = 1920
        mock_gumnut_asset.height = 1080
        mock_gumnut_asset.duration = None
        mock_gumnut_asset.file_size_bytes = 1024
        mock_gumnut_asset.metadata = None
        mock_gumnut_asset.people = []
        mock_gumnut_asset.trashed_at = None

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
        mock_gumnut_asset.checksum_sha1 = "PaDX6+c+Lhjpm5/ciXUROL1ryaU="
        mock_gumnut_asset.thumbhash = None
        mock_gumnut_asset.original_file_name = "video.mp4"
        mock_gumnut_asset.created_at = datetime.now(timezone.utc)
        mock_gumnut_asset.updated_at = datetime.now(timezone.utc)
        mock_gumnut_asset.mime_type = "video/mp4"
        mock_gumnut_asset.width = 1920
        mock_gumnut_asset.height = 1080
        mock_gumnut_asset.duration = None
        mock_gumnut_asset.file_size_bytes = 10240
        mock_gumnut_asset.metadata = None
        mock_gumnut_asset.people = []
        mock_gumnut_asset.trashed_at = None

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

        from routers.api.assets import _pending_emit_tasks

        with (
            patch("routers.api.assets.is_live_photo_video", return_value=False),
            patch("routers.api.assets.emit_user_event", new_callable=AsyncMock),
            patch("routers.api.assets._VIDEO_EMIT_DELAY_SECONDS", 0.0),
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

            # Drain the deferred-emit task spawned for the video upload so it
            # completes inside the patched-mocks scope.
            if _pending_emit_tasks:
                await asyncio.gather(*list(_pending_emit_tasks))

    @pytest.mark.anyio
    async def test_video_upload_defers_websocket_events(
        self, sample_uuid, mock_current_user
    ):
        """Video uploads defer WebSocket emission until after the configured delay."""
        mock_gumnut_asset = Mock()
        mock_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_gumnut_asset.checksum = "abc123"
        mock_gumnut_asset.checksum_sha1 = "PaDX6+c+Lhjpm5/ciXUROL1ryaU="
        mock_gumnut_asset.thumbhash = None
        mock_gumnut_asset.original_file_name = "video.mp4"
        mock_gumnut_asset.created_at = datetime.now(timezone.utc)
        mock_gumnut_asset.updated_at = datetime.now(timezone.utc)
        mock_gumnut_asset.local_datetime = mock_gumnut_asset.created_at
        mock_gumnut_asset.file_created_at = mock_gumnut_asset.created_at
        mock_gumnut_asset.file_modified_at = mock_gumnut_asset.updated_at
        mock_gumnut_asset.mime_type = "video/mp4"
        mock_gumnut_asset.width = 1920
        mock_gumnut_asset.height = 1080
        mock_gumnut_asset.duration = None
        mock_gumnut_asset.file_size_bytes = 10240
        mock_gumnut_asset.metadata = None
        mock_gumnut_asset.people = []
        mock_gumnut_asset.trashed_at = None

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

        from routers.api.assets import _pending_emit_tasks

        with (
            patch("routers.api.assets.is_live_photo_video", return_value=False),
            patch(
                "routers.api.assets.emit_user_event", new_callable=AsyncMock
            ) as mock_emit,
            patch("routers.api.assets._VIDEO_EMIT_DELAY_SECONDS", 0.0),
        ):
            await upload_asset(
                request=request,
                client=mock_client,
                current_user=mock_current_user,
                settings=settings,
            )

            # Emission is deferred — nothing fired before we yield to the loop.
            assert mock_emit.call_count == 0
            assert len(_pending_emit_tasks) == 1

            await asyncio.gather(*list(_pending_emit_tasks))

            assert mock_emit.call_count == 2
            first_call = mock_emit.call_args_list[0]
            assert first_call[0][0] == WebSocketEvent.UPLOAD_SUCCESS
            assert first_call[0][1] == mock_current_user.id

            second_call = mock_emit.call_args_list[1]
            assert second_call[0][0] == WebSocketEvent.ASSET_UPLOAD_READY_V1
            assert second_call[0][1] == mock_current_user.id

            # done_callback drops the completed task from the strong-ref set.
            assert len(_pending_emit_tasks) == 0

    @pytest.mark.anyio
    async def test_upload_asset_websocket_error_does_not_fail_upload(
        self, sample_uuid, mock_current_user
    ):
        """Test that WebSocket emission errors don't fail the upload."""
        mock_gumnut_asset = Mock()
        mock_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_gumnut_asset.checksum = "abc123"
        mock_gumnut_asset.checksum_sha1 = "PaDX6+c+Lhjpm5/ciXUROL1ryaU="
        mock_gumnut_asset.thumbhash = None
        mock_gumnut_asset.original_file_name = "test.jpg"
        mock_gumnut_asset.created_at = datetime.now(timezone.utc)
        mock_gumnut_asset.updated_at = datetime.now(timezone.utc)
        mock_gumnut_asset.mime_type = "image/jpeg"
        mock_gumnut_asset.width = 1920
        mock_gumnut_asset.height = 1080
        mock_gumnut_asset.duration = None
        mock_gumnut_asset.file_size_bytes = 1024
        mock_gumnut_asset.metadata = None
        mock_gumnut_asset.people = []
        mock_gumnut_asset.trashed_at = None

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

    @pytest.mark.anyio
    async def test_streaming_upload_client_disconnect(self, mock_current_user):
        """A client disconnect on the streaming path returns 499 rather than being
        mapped to a 500/502 by the pipeline's broad error handler."""
        from starlette.requests import ClientDisconnect

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
        mock_pipeline_instance.execute = AsyncMock(side_effect=ClientDisconnect())

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
        assert result.status_code == 499


class TestUpdateAssets:
    """Test the bulk asset-metadata edit endpoint.

    `PUT /api/assets` forwards a subset of `AssetBulkUpdateDto` to the
    Photos API `bulk_update_assets` call. In-scope fields: `description`,
    paired `latitude` + `longitude`, and the capture time via one of three
    mutually exclusive datetime modes — absolute `dateTimeOriginal` (with
    optional `timeZone`), per-asset `dateTimeRelative` shift, or standalone
    `timeZone` reinterpret. The two per-asset modes read each asset's current
    `original_datetime` first (`client.assets.list(ids=...)`), then write a
    heterogeneous per-item change. Out-of-scope fields (`isFavorite`,
    `rating`, `visibility`, `duplicateId`) are silently ignored. Conflicting
    datetime modes are rejected with 422.
    """

    @staticmethod
    def _assert_calls_homogeneous_change(
        mock_call: AsyncMock,
        expected_ids: list[UUID],
        expected_change: dict[str, Any],
    ) -> None:
        """Assert bulk_update_assets was called once with the expected updates."""
        mock_call.assert_awaited_once()
        call = mock_call.await_args
        assert call is not None
        updates = call.kwargs["updates"]
        assert [item["id"] for item in updates] == [
            uuid_to_gumnut_asset_id(uid) for uid in expected_ids
        ]
        for item in updates:
            assert item["change"] == expected_change

    @staticmethod
    def _change_by_id(mock_call: AsyncMock) -> dict[str, Any]:
        """Map gumnut id → change from a single bulk_update_assets call."""
        mock_call.assert_awaited_once()
        call = mock_call.await_args
        assert call is not None
        return {item["id"]: item["change"] for item in call.kwargs["updates"]}

    @staticmethod
    def _mock_read(assets_by_uuid: dict[UUID, datetime | None]) -> Mock:
        """Build a `client.assets.list` mock returning the given current
        `original_datetime` per asset.

        A `None` value models an asset whose metadata carries no capture time
        (skipped for the per-asset datetime rewrite). Returns a `Mock` whose
        `return_value` is a `MockSyncCursorPage` so a single call is replayed;
        callers wanting per-chunk pages set `side_effect` themselves.
        """
        from tests.conftest import MockSyncCursorPage

        page_assets = []
        for uid, dt in assets_by_uuid.items():
            asset = Mock()
            asset.id = uuid_to_gumnut_asset_id(uid)
            asset.metadata = Mock(original_datetime=dt)
            page_assets.append(asset)
        return Mock(return_value=MockSyncCursorPage(page_assets))

    @pytest.mark.anyio
    async def test_update_assets_description_round_trips(self):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4(), uuid4()]
        request = AssetBulkUpdateDto(ids=ids, description="hello")

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets, ids, {"description": "hello"}
        )

    @pytest.mark.anyio
    async def test_update_assets_description_null_clears(self):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto.model_validate(
            {"ids": [str(uid) for uid in ids], "description": None}
        )

        await update_assets(request, client=mock_client)

        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets, ids, {"description": None}
        )

    @pytest.mark.anyio
    async def test_update_assets_lat_lon_round_trips(self):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto(ids=ids, latitude=37.7749, longitude=-122.4194)

        await update_assets(request, client=mock_client)

        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets,
            ids,
            {"latitude": 37.7749, "longitude": -122.4194},
        )

    @pytest.mark.anyio
    async def test_update_assets_lat_lon_both_null_clears(self):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto.model_validate(
            {
                "ids": [str(uid) for uid in ids],
                "latitude": None,
                "longitude": None,
            }
        )

        await update_assets(request, client=mock_client)

        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets,
            ids,
            {"latitude": None, "longitude": None},
        )

    @pytest.mark.anyio
    async def test_update_assets_only_latitude_is_422(self):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock()
        request = AssetBulkUpdateDto(ids=[uuid4()], latitude=37.7749)

        with pytest.raises(HTTPException) as exc_info:
            await update_assets(request, client=mock_client)

        assert exc_info.value.status_code == 422
        mock_client.assets.bulk_update_assets.assert_not_awaited()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "payload_extras",
        [
            {"latitude": None, "longitude": 1.0},
            {"latitude": 1.0, "longitude": None},
        ],
    )
    async def test_update_assets_half_cleared_coords_is_422(self, payload_extras):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock()
        ids = [uuid4()]
        request = AssetBulkUpdateDto.model_validate(
            {"ids": [str(uid) for uid in ids], **payload_extras}
        )

        with pytest.raises(HTTPException) as exc_info:
            await update_assets(request, client=mock_client)

        assert exc_info.value.status_code == 422
        mock_client.assets.bulk_update_assets.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_assets_datetime_with_offset_round_trips(self):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto(
            ids=ids, dateTimeOriginal="2024-06-15T14:30:00-07:00"
        )

        await update_assets(request, client=mock_client)

        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets,
            ids,
            {
                "original_datetime": datetime(
                    2024, 6, 15, 14, 30, 0, tzinfo=timezone(-timedelta(hours=7))
                )
            },
        )

    @pytest.mark.anyio
    async def test_update_assets_datetime_naive_round_trips(self):
        # Naive datetime (no offset, no Z). The backend accepts naive; the
        # adapter does not synthesise an offset.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto(ids=ids, dateTimeOriginal="2024-06-15T14:30:00")

        await update_assets(request, client=mock_client)

        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets,
            ids,
            {"original_datetime": datetime(2024, 6, 15, 14, 30, 0)},
        )

    @pytest.mark.anyio
    async def test_update_assets_datetime_null_clears(self):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto.model_validate(
            {"ids": [str(uid) for uid in ids], "dateTimeOriginal": None}
        )

        await update_assets(request, client=mock_client)

        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets,
            ids,
            {"original_datetime": None},
        )

    @pytest.mark.anyio
    async def test_update_assets_invalid_datetime_is_422(self):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock()
        request = AssetBulkUpdateDto(ids=[uuid4()], dateTimeOriginal="not-a-datetime")

        with pytest.raises(HTTPException) as exc_info:
            await update_assets(request, client=mock_client)

        assert exc_info.value.status_code == 422
        mock_client.assets.bulk_update_assets.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_assets_datetime_with_timezone_combines(self):
        # Web modal sends naive `dateTimeOriginal` + IANA `timeZone`; we
        # localize wall-clock to that zone before forwarding.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto(
            ids=ids,
            dateTimeOriginal="2024-06-15T14:30:00",
            timeZone="America/Los_Angeles",
        )

        await update_assets(request, client=mock_client)

        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets,
            ids,
            {
                "original_datetime": datetime(
                    2024, 6, 15, 14, 30, 0, tzinfo=ZoneInfo("America/Los_Angeles")
                )
            },
        )

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "tz_name",
        [
            "Mars/Olympus_Mons",  # ZoneInfoNotFoundError: valid-format but unknown
            "",  # ValueError: empty
            "/Etc/UTC",  # ValueError: absolute path
            "../etc/passwd",  # ValueError: non-normalized path
        ],
    )
    async def test_update_assets_invalid_timezone_is_422(self, tz_name: str):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock()
        request = AssetBulkUpdateDto(
            ids=[uuid4()],
            dateTimeOriginal="2024-06-15T14:30:00",
            timeZone=tz_name,
        )

        with pytest.raises(HTTPException) as exc_info:
            await update_assets(request, client=mock_client)

        assert exc_info.value.status_code == 422
        mock_client.assets.bulk_update_assets.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_assets_timezone_alone_reinterprets_per_asset(self):
        # Standalone `timeZone` re-anchors each asset's existing wall-clock in
        # the given zone: read current `original_datetime`, swap the tzinfo
        # (preserve the clock digits), write per-asset.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        a, b = uuid4(), uuid4()
        mock_client.assets.list = self._mock_read(
            {
                a: datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
                b: datetime(2020, 1, 2, 9, 0, 0, tzinfo=timezone.utc),
            }
        )
        request = AssetBulkUpdateDto(ids=[a, b], timeZone="America/Los_Angeles")

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        la = ZoneInfo("America/Los_Angeles")
        by_id = self._change_by_id(mock_client.assets.bulk_update_assets)
        assert by_id[uuid_to_gumnut_asset_id(a)] == {
            "original_datetime": datetime(2024, 6, 15, 14, 30, 0, tzinfo=la)
        }
        assert by_id[uuid_to_gumnut_asset_id(b)] == {
            "original_datetime": datetime(2020, 1, 2, 9, 0, 0, tzinfo=la)
        }

    @pytest.mark.anyio
    async def test_update_assets_relative_datetime_shifts_per_asset(self):
        # `dateTimeRelative` shifts each asset's existing datetime by N
        # seconds: read current values, add the delta, write per-asset.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        a, b = uuid4(), uuid4()
        base_a = datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        base_b = datetime(2020, 1, 2, 9, 0, 0)
        mock_client.assets.list = self._mock_read({a: base_a, b: base_b})
        request = AssetBulkUpdateDto(ids=[a, b], dateTimeRelative=3600.0)

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        by_id = self._change_by_id(mock_client.assets.bulk_update_assets)
        assert by_id[uuid_to_gumnut_asset_id(a)] == {
            "original_datetime": base_a + timedelta(seconds=3600)
        }
        assert by_id[uuid_to_gumnut_asset_id(b)] == {
            "original_datetime": base_b + timedelta(seconds=3600)
        }

    @pytest.mark.anyio
    async def test_update_assets_per_asset_skips_null_datetime(self):
        # An asset with no existing `original_datetime` (and one absent from
        # the read entirely) is skipped for the datetime rewrite; with no
        # other in-scope field it drops out of the write. The asset with a
        # base datetime is still updated.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        has_dt, no_dt, missing = uuid4(), uuid4(), uuid4()
        base = datetime(2024, 6, 15, 14, 30, 0)
        # `missing` is omitted from the read entirely; `no_dt` has null metadata dt.
        mock_client.assets.list = self._mock_read({has_dt: base, no_dt: None})
        request = AssetBulkUpdateDto(
            ids=[has_dt, no_dt, missing], dateTimeRelative=60.0
        )

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets,
            [has_dt],
            {"original_datetime": base + timedelta(seconds=60)},
        )

    @pytest.mark.anyio
    async def test_update_assets_per_asset_keeps_homogeneous_fields_when_skipping(
        self,
    ):
        # When a per-asset datetime mode is mixed with a homogeneous field,
        # an asset skipped for the datetime rewrite still receives the
        # homogeneous field rather than dropping out.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        has_dt, no_dt = uuid4(), uuid4()
        base = datetime(2024, 6, 15, 14, 30, 0)
        mock_client.assets.list = self._mock_read({has_dt: base, no_dt: None})
        request = AssetBulkUpdateDto(
            ids=[has_dt, no_dt], dateTimeRelative=60.0, description="caption"
        )

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        by_id = self._change_by_id(mock_client.assets.bulk_update_assets)
        assert by_id[uuid_to_gumnut_asset_id(has_dt)] == {
            "description": "caption",
            "original_datetime": base + timedelta(seconds=60),
        }
        assert by_id[uuid_to_gumnut_asset_id(no_dt)] == {"description": "caption"}

    @pytest.mark.anyio
    async def test_update_assets_per_asset_reads_all_states_incl_trashed(self):
        # The per-asset read must use state="all" so a trashed (non-live) asset
        # is still rewritten, matching the homogeneous path which forwards every
        # id regardless of trash state. The default live-only filter would drop
        # trashed ids from the read and silently skip them.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        trashed = uuid4()
        base = datetime(2024, 6, 15, 14, 30, 0)
        mock_client.assets.list = self._mock_read({trashed: base})
        request = AssetBulkUpdateDto(ids=[trashed], dateTimeRelative=3600.0)

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        mock_client.assets.list.assert_called_once_with(
            state="all",
            ids=[uuid_to_gumnut_asset_id(trashed)],
            limit=1,
        )
        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets,
            [trashed],
            {"original_datetime": base + timedelta(seconds=3600)},
        )

    @pytest.mark.anyio
    async def test_update_assets_per_asset_zero_writable_chunk_skips_write(self):
        # A chunk where every asset is non-writable in a per-asset datetime mode
        # (no current `original_datetime`) and no homogeneous field is present
        # produces zero updates, so `bulk_update_assets` is never awaited — pins
        # the `if not updates: continue` branch.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        a, b = uuid4(), uuid4()
        mock_client.assets.list = self._mock_read({a: None, b: None})
        request = AssetBulkUpdateDto(ids=[a, b], dateTimeRelative=60.0)

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        mock_client.assets.bulk_update_assets.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_assets_relative_with_absolute_is_422(self):
        # The three datetime modes are mutually exclusive — relative + absolute
        # is ambiguous and rejected before any network call.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock()
        mock_client.assets.list = AsyncMock()
        request = AssetBulkUpdateDto(
            ids=[uuid4()],
            dateTimeRelative=3600.0,
            dateTimeOriginal="2024-06-15T14:30:00",
        )

        with pytest.raises(HTTPException) as exc_info:
            await update_assets(request, client=mock_client)

        assert exc_info.value.status_code == 422
        mock_client.assets.bulk_update_assets.assert_not_awaited()
        mock_client.assets.list.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_assets_relative_with_timezone_is_422(self):
        # relative + standalone timeZone is ambiguous and rejected.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock()
        mock_client.assets.list = AsyncMock()
        request = AssetBulkUpdateDto(
            ids=[uuid4()], dateTimeRelative=3600.0, timeZone="America/Los_Angeles"
        )

        with pytest.raises(HTTPException) as exc_info:
            await update_assets(request, client=mock_client)

        assert exc_info.value.status_code == 422
        mock_client.assets.bulk_update_assets.assert_not_awaited()
        mock_client.assets.list.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_assets_standalone_invalid_timezone_is_422(self):
        # A bad standalone-timeZone name 422s before any read, not after.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock()
        mock_client.assets.list = AsyncMock()
        request = AssetBulkUpdateDto(ids=[uuid4()], timeZone="Mars/Olympus_Mons")

        with pytest.raises(HTTPException) as exc_info:
            await update_assets(request, client=mock_client)

        assert exc_info.value.status_code == 422
        mock_client.assets.list.assert_not_awaited()
        mock_client.assets.bulk_update_assets.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_assets_relative_datetime_null_is_ignored(self):
        # Explicit null for dateTimeRelative is "field not set", not a 422 —
        # some Immich client SDKs emit null for fields they don't intend to
        # change, and the rejection only triggers when non-null.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto.model_validate(
            {
                "ids": [str(uid) for uid in ids],
                "dateTimeRelative": None,
                "description": "x",
            }
        )

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets, ids, {"description": "x"}
        )

    @pytest.mark.anyio
    async def test_update_assets_timezone_null_is_ignored(self):
        # Explicit null for timeZone (without dateTimeOriginal) is "field not
        # set", not the standalone-timeZone 422 — clients send null for
        # untouched fields and the adapter must not surface that as an error.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto.model_validate(
            {
                "ids": [str(uid) for uid in ids],
                "timeZone": None,
                "description": "x",
            }
        )

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets, ids, {"description": "x"}
        )

    @pytest.mark.anyio
    async def test_update_assets_empty_ids_no_call(self):
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock()
        request = AssetBulkUpdateDto(ids=[], description="hello")

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        mock_client.assets.bulk_update_assets.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_assets_no_in_scope_fields_no_call(self):
        # Only out-of-scope fields → adapter returns 204 without calling the
        # backend; client UIs don't show errors for unsupported edits.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock()
        request = AssetBulkUpdateDto(
            ids=[uuid4()],
            isFavorite=True,
            rating=4.0,
            visibility=AssetVisibility.archive,
            duplicateId=str(uuid4()),
        )

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        mock_client.assets.bulk_update_assets.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_assets_out_of_scope_fields_dropped_when_mixed(self):
        # When in-scope and out-of-scope fields are mixed, only in-scope
        # values appear in the bulk-update body.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto(
            ids=ids,
            description="caption",
            isFavorite=True,
            rating=4.0,
        )

        await update_assets(request, client=mock_client)

        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets, ids, {"description": "caption"}
        )

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "total, expected_chunks",
        [
            # Exact-boundary cases per docs/references/code-practices.md to
            # catch off-by-one regressions a hand-rolled `if len > N` split
            # would introduce. BULK_CHUNK_SIZE=100; the third case verifies
            # the "second chunk is a single element" edge.
            (100, [100]),
            (101, [100, 1]),
            (205, [100, 100, 5]),
        ],
    )
    async def test_update_assets_chunks_when_over_cap(
        self, total: int, expected_chunks: list[int]
    ):
        """Request larger than the per-call cap is split into chunks."""
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)

        ids = [uuid4() for _ in range(total)]
        request = AssetBulkUpdateDto(ids=ids, description="caption")

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        chunk_sizes = [
            len(call.kwargs["updates"])
            for call in mock_client.assets.bulk_update_assets.await_args_list
        ]
        assert chunk_sizes == expected_chunks
        # Ids land in the chunks in input order; homogeneous change replicates per item.
        flat_ids = [
            item["id"]
            for call in mock_client.assets.bulk_update_assets.await_args_list
            for item in call.kwargs["updates"]
        ]
        assert flat_ids == [uuid_to_gumnut_asset_id(uid) for uid in ids]
        for call in mock_client.assets.bulk_update_assets.await_args_list:
            for item in call.kwargs["updates"]:
                assert item["change"] == {"description": "caption"}

    @pytest.mark.anyio
    async def test_update_assets_per_asset_chunks_read_and_write(self):
        """Per-asset datetime mode reads and writes once per chunk.

        The bulk GET (`assets.list`) is chunked alongside the bulk PATCH, so a
        101-id relative shift is two reads + two writes — not a per-asset GET
        fan-out — with ids preserved in input order across chunks.
        """
        from tests.conftest import MockSyncCursorPage
        from routers.utils.gumnut_client import BULK_CHUNK_SIZE

        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)

        ids = [uuid4() for _ in range(BULK_CHUNK_SIZE + 1)]
        base = datetime(2024, 6, 15, 14, 30, 0)

        def _page_for(*_args, **kwargs):
            assets = []
            for gid in kwargs["ids"]:
                asset = Mock()
                asset.id = gid
                asset.metadata = Mock(original_datetime=base)
                assets.append(asset)
            return MockSyncCursorPage(assets)

        mock_client.assets.list = Mock(side_effect=_page_for)
        request = AssetBulkUpdateDto(ids=ids, dateTimeRelative=60.0)

        result = await update_assets(request, client=mock_client)

        assert result.status_code == 204
        read_sizes = [
            len(call.kwargs["ids"]) for call in mock_client.assets.list.call_args_list
        ]
        assert read_sizes == [100, 1]
        write_sizes = [
            len(call.kwargs["updates"])
            for call in mock_client.assets.bulk_update_assets.await_args_list
        ]
        assert write_sizes == [100, 1]
        flat_ids = [
            item["id"]
            for call in mock_client.assets.bulk_update_assets.await_args_list
            for item in call.kwargs["updates"]
        ]
        assert flat_ids == [uuid_to_gumnut_asset_id(uid) for uid in ids]
        for call in mock_client.assets.bulk_update_assets.await_args_list:
            for item in call.kwargs["updates"]:
                assert item["change"] == {
                    "original_datetime": base + timedelta(seconds=60)
                }

    @pytest.mark.anyio
    async def test_update_assets_aware_datetime_with_timezone_re_anchors(self):
        # Pins the docstring claim that aware inputs are re-anchored:
        # wall-clock digits preserved, tz replaced. Without this, a future
        # switch to astimezone would silently convert the moment in time
        # instead.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto(
            ids=ids,
            dateTimeOriginal="2024-06-15T14:30:00-07:00",
            timeZone="America/New_York",
        )

        await update_assets(request, client=mock_client)

        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets,
            ids,
            {
                "original_datetime": datetime(
                    2024, 6, 15, 14, 30, 0, tzinfo=ZoneInfo("America/New_York")
                )
            },
        )

    @pytest.mark.anyio
    async def test_update_assets_combined_in_scope_fields(self):
        # All in-scope fields in one request — guards against a future
        # refactor of `_build_bulk_metadata_change` that handles each field
        # in isolation but breaks when they're composed.
        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        ids = [uuid4()]
        request = AssetBulkUpdateDto(
            ids=ids,
            description="caption",
            latitude=37.7749,
            longitude=-122.4194,
            dateTimeOriginal="2024-06-15T14:30:00",
            timeZone="America/Los_Angeles",
        )

        await update_assets(request, client=mock_client)

        self._assert_calls_homogeneous_change(
            mock_client.assets.bulk_update_assets,
            ids,
            {
                "description": "caption",
                "latitude": 37.7749,
                "longitude": -122.4194,
                "original_datetime": datetime(
                    2024, 6, 15, 14, 30, 0, tzinfo=ZoneInfo("America/Los_Angeles")
                ),
            },
        )

    @pytest.mark.anyio
    async def test_update_assets_propagates_sdk_error(self):
        """SDK errors on bulk-update bubble to the global GumnutError handler.

        Pins the no-swallow contract: a future refactor that wraps the SDK
        call in try/except (e.g. to skip a failing chunk mid-batch) must
        break this test.
        """
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.bulk_update_assets = AsyncMock(
            side_effect=make_sdk_status_error(500, "boom")
        )

        request = AssetBulkUpdateDto(ids=[uuid4()], description="caption")

        with pytest.raises(APIStatusError):
            await update_assets(request, client=mock_client)

    @pytest.mark.anyio
    async def test_update_assets_multi_chunk_failure_leaves_prior_chunks_committed(
        self,
    ):
        """Pin the no-rollback contract for cross-chunk failures.

        The handler docstring (and `docs/references/code-practices.md`) call
        out that SDK atomicity holds per call but not across chunks: chunk N
        (N≥2) raising leaves chunks 1..N-1 already committed and the error
        propagates as one 5xx. A future refactor that wraps the per-chunk
        await in try/except (skip-on-failure) or switches to `asyncio.gather`
        with `return_exceptions` would change failure semantics and must break
        this test.
        """
        from gumnut import APIStatusError
        from routers.utils.gumnut_client import BULK_CHUNK_SIZE
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        # Chunk 1 succeeds, chunk 2 raises — exercises the partial-commit shape.
        mock_client.assets.bulk_update_assets = AsyncMock(
            side_effect=[None, make_sdk_status_error(500, "boom")]
        )

        ids = [uuid4() for _ in range(BULK_CHUNK_SIZE + 1)]
        request = AssetBulkUpdateDto(ids=ids, description="caption")

        with pytest.raises(APIStatusError):
            await update_assets(request, client=mock_client)

        # Awaited exactly twice: chunk 1 (committed) + chunk 2 (raised).
        # No rollback attempt — the handler does not re-call chunk 1 to undo.
        assert mock_client.assets.bulk_update_assets.await_count == 2


class TestUpdateAsset:
    """Test the single-asset metadata edit endpoint.

    `PUT /api/assets/{id}` forwards a subset of `UpdateAssetDto` to the Photos
    API `update_asset` PATCH: `description`, paired `latitude` + `longitude`,
    and `dateTimeOriginal`. Out-of-scope fields (`isFavorite`, `rating`,
    `visibility`, `livePhotoVideoId`) are silently ignored — the request
    succeeds, the adapter just doesn't act on parts the Photos API doesn't
    model. An empty payload returns the asset unchanged without an SDK call.
    """

    @pytest.mark.anyio
    async def test_update_asset_description_round_trips(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto(description="hello")

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid), description="hello"
        )
        assert result.id == str(sample_uuid)

    @pytest.mark.anyio
    async def test_update_asset_description_null_clears(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto.model_validate({"description": None})

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid), description=None
        )

    @pytest.mark.anyio
    async def test_update_asset_empty_description_lets_backend_reject(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        # Empty string is forwarded; the backend rejects it with 422 via the
        # global GumnutError handler. The adapter doesn't pre-validate length.
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto(description="")

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid), description=""
        )

    @pytest.mark.anyio
    async def test_update_asset_lat_lon_round_trips(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto(latitude=37.7749, longitude=-122.4194)

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid),
            latitude=37.7749,
            longitude=-122.4194,
        )

    @pytest.mark.anyio
    async def test_update_asset_lat_lon_both_null_clears(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto.model_validate({"latitude": None, "longitude": None})

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid),
            latitude=None,
            longitude=None,
        )

    @pytest.mark.anyio
    async def test_update_asset_only_latitude_is_422(
        self, sample_uuid, mock_current_user
    ):
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock()

        request = UpdateAssetDto(latitude=37.7749)

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as exc_info:
                await update_asset(
                    sample_uuid,
                    request,
                    client=mock_client,
                    current_user=mock_current_user,
                )

        assert exc_info.value.status_code == 422
        mock_client.assets.update_asset.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_asset_only_longitude_is_422(
        self, sample_uuid, mock_current_user
    ):
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock()

        request = UpdateAssetDto(longitude=-122.4194)

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as exc_info:
                await update_asset(
                    sample_uuid,
                    request,
                    client=mock_client,
                    current_user=mock_current_user,
                )

        assert exc_info.value.status_code == 422
        mock_client.assets.update_asset.assert_not_awaited()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "payload",
        [
            {"latitude": None, "longitude": 1.0},
            {"latitude": 1.0, "longitude": None},
        ],
    )
    async def test_update_asset_half_cleared_coords_is_422(
        self, sample_uuid, mock_current_user, payload
    ):
        # XOR-style check is symmetric in latitude/longitude — cover both
        # directions so a typo that broke one side wouldn't pass.
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock()

        request = UpdateAssetDto.model_validate(payload)

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as exc_info:
                await update_asset(
                    sample_uuid,
                    request,
                    client=mock_client,
                    current_user=mock_current_user,
                )

        assert exc_info.value.status_code == 422
        mock_client.assets.update_asset.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_asset_datetime_with_offset_round_trips(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto(dateTimeOriginal="2024-06-15T14:30:00-07:00")

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid),
            original_datetime=datetime(
                2024, 6, 15, 14, 30, 0, tzinfo=timezone(-timedelta(hours=7))
            ),
        )

    @pytest.mark.anyio
    async def test_update_asset_datetime_naive_round_trips(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        # Naive datetime (no offset, no Z). The backend accepts naive; the
        # adapter does not synthesise an offset.
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto(dateTimeOriginal="2024-06-15T14:30:00")

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid),
            original_datetime=datetime(2024, 6, 15, 14, 30, 0),
        )

    @pytest.mark.anyio
    async def test_update_asset_datetime_z_suffix_round_trips(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto(dateTimeOriginal="2024-06-15T14:30:00Z")

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid),
            original_datetime=datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
        )

    @pytest.mark.anyio
    async def test_update_asset_datetime_null_clears(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto.model_validate({"dateTimeOriginal": None})

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid), original_datetime=None
        )

    @pytest.mark.anyio
    async def test_update_asset_invalid_datetime_is_422(
        self, sample_uuid, mock_current_user
    ):
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock()

        request = UpdateAssetDto(dateTimeOriginal="not-a-datetime")

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as exc_info:
                await update_asset(
                    sample_uuid,
                    request,
                    client=mock_client,
                    current_user=mock_current_user,
                )

        assert exc_info.value.status_code == 422
        mock_client.assets.update_asset.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_asset_empty_payload_no_sdk_call(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        # Empty DTO + retrieve path: asset is fetched via get_asset_info, no
        # PATCH is sent, no websocket fires.
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock()
        mock_client.assets.retrieve = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto()

        with patch(
            "routers.api.assets.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            result = await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_not_awaited()
        mock_client.assets.retrieve.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid)
        )
        mock_emit.assert_not_awaited()
        assert result.id == str(sample_uuid)

    @pytest.mark.anyio
    async def test_update_asset_out_of_scope_fields_no_sdk_call(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        # Out-of-scope fields (favorite/rating/visibility/livePhotoVideoId)
        # by themselves don't trigger a PATCH — the request still succeeds
        # via the retrieve path so client UIs don't show errors.
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock()
        mock_client.assets.retrieve = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto(
            isFavorite=True,
            rating=5.0,
            visibility=AssetVisibility.archive,
            livePhotoVideoId=uuid4(),
        )

        with patch(
            "routers.api.assets.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_not_awaited()
        mock_emit.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_asset_out_of_scope_fields_ignored_when_mixed(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        # When a mix of in-scope and out-of-scope fields is sent, only the
        # in-scope kwargs reach the SDK.
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto(
            description="caption",
            isFavorite=True,
            rating=4.0,
        )

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        mock_client.assets.update_asset.assert_awaited_once_with(
            uuid_to_gumnut_asset_id(sample_uuid), description="caption"
        )

    @pytest.mark.anyio
    async def test_update_asset_emits_websocket_event(
        self, sample_uuid, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.id = uuid_to_gumnut_asset_id(sample_uuid)
        mock_client = Mock()
        mock_client.assets.update_asset = AsyncMock(return_value=sample_gumnut_asset)

        request = UpdateAssetDto(description="new caption")

        with patch(
            "routers.api.assets.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            result = await update_asset(
                sample_uuid,
                request,
                client=mock_client,
                current_user=mock_current_user,
            )

        # Payload is the converted AssetResponseDto, not a bare ID string —
        # matches upstream Immich + the web client signature.
        mock_emit.assert_awaited_once_with(
            WebSocketEvent.ASSET_UPDATE, mock_current_user.id, result
        )


class TestDeleteAssets:
    """Test the delete_assets endpoint.

    The handler branches on ``force``: ``True`` → bulk hard-delete via
    ``client.delete("/api/assets", body=...)`` with one ``on_asset_delete``
    per id. ``False``/absent → bulk soft-delete via
    ``client.post("/api/assets/trash", body=...)`` with one batched
    ``on_asset_trash`` per chunk.
    """

    @pytest.mark.anyio
    async def test_delete_assets_force_false_calls_trash_endpoint(self):
        """force=False routes to POST /api/assets/trash with the full id list."""
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        asset_ids = [uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)
        current_user_id = uuid4()

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert result.status_code == 204
        mock_client.post.assert_awaited_once()
        call = mock_client.post.await_args
        assert call.args[0] == "/api/assets/trash"
        body = call.kwargs["body"]
        assert set(body["ids"]) == {uuid_to_gumnut_asset_id(uid) for uid in asset_ids}

    @pytest.mark.anyio
    async def test_delete_assets_force_absent_treated_as_soft_delete(self):
        """force omitted (Immich's native default) routes to trash, not hard-delete."""
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)
        mock_client.delete = AsyncMock(return_value=None)

        asset_ids = [uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids)
        current_user_id = uuid4()

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert result.status_code == 204
        mock_client.post.assert_awaited_once()
        mock_client.delete.assert_not_awaited()

    @pytest.mark.anyio
    async def test_delete_assets_force_true_calls_bulk_delete_endpoint(self):
        """force=True routes to bulk DELETE /api/assets."""
        mock_client = Mock()
        mock_client.delete = AsyncMock(return_value=None)

        asset_ids = [uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=True)
        current_user_id = uuid4()

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert result.status_code == 204
        mock_client.delete.assert_awaited_once()
        call = mock_client.delete.await_args
        assert call.args[0] == "/api/assets"
        body = call.kwargs["body"]
        assert set(body["ids"]) == {uuid_to_gumnut_asset_id(uid) for uid in asset_ids}

    @pytest.mark.anyio
    async def test_delete_assets_force_false_emits_single_batched_trash_event(self):
        """A single bulk soft-delete fires one on_asset_trash with the full id array."""
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        asset_ids = [uuid4(), uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)
        current_user_id = uuid4()

        with patch(
            "routers.api.assets.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert mock_emit.await_count == 1
        event, user_id, payload = mock_emit.await_args_list[0].args
        assert event == WebSocketEvent.ASSET_TRASH
        assert user_id == str(current_user_id)
        assert payload == [str(uid) for uid in asset_ids]

    @pytest.mark.anyio
    async def test_delete_assets_force_true_emits_one_delete_event_per_id(self):
        """A bulk hard-delete fires one on_asset_delete per id (single-id wire shape)."""
        mock_client = Mock()
        mock_client.delete = AsyncMock(return_value=None)

        asset_ids = [uuid4(), uuid4(), uuid4()]
        request = AssetBulkDeleteDto(ids=asset_ids, force=True)
        current_user_id = uuid4()

        # `_bulk_permanent_delete` calls `emit_user_event_per_id`, which fans
        # out `emit_user_event` once per id. Patch at the websockets module
        # so the per-id call count is observable.
        with patch(
            "services.websockets.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert mock_emit.await_count == 3
        for i, call in enumerate(mock_emit.await_args_list):
            event, user_id, payload = call.args
            assert event == WebSocketEvent.ASSET_DELETE
            assert user_id == str(current_user_id)
            assert payload == str(asset_ids[i])

    @pytest.mark.anyio
    async def test_delete_assets_chunks_when_over_cap(self):
        """Request larger than the per-call cap is split into chunks."""
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        # 250 ids → 100 + 100 + 50 across three trash calls.
        asset_ids = [uuid4() for _ in range(250)]
        request = AssetBulkDeleteDto(ids=asset_ids, force=False)
        current_user_id = uuid4()

        with patch(
            "routers.api.assets.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert result.status_code == 204
        assert mock_client.post.await_count == 3
        chunk_sizes = [
            len(call.kwargs["body"]["ids"]) for call in mock_client.post.await_args_list
        ]
        assert chunk_sizes == [100, 100, 50]
        # One batched on_asset_trash per chunk.
        assert mock_emit.await_count == 3

    @pytest.mark.anyio
    async def test_delete_assets_force_true_chunks_and_emits_per_id(self):
        """force=True over the cap chunks bulk DELETE and emits one event per id."""
        mock_client = Mock()
        mock_client.delete = AsyncMock(return_value=None)

        asset_ids = [uuid4() for _ in range(250)]
        request = AssetBulkDeleteDto(ids=asset_ids, force=True)
        current_user_id = uuid4()

        # `_bulk_permanent_delete` calls `emit_user_event_per_id`, which fans
        # out `emit_user_event` once per id. Patch at the websockets module
        # so the per-id call count is observable.
        with patch(
            "services.websockets.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert result.status_code == 204
        assert mock_client.delete.await_count == 3
        chunk_sizes = [
            len(call.kwargs["body"]["ids"])
            for call in mock_client.delete.await_args_list
        ]
        assert chunk_sizes == [100, 100, 50]
        # 250 per-id on_asset_delete events across all chunks.
        assert mock_emit.await_count == 250

    @pytest.mark.anyio
    async def test_delete_assets_websocket_error_does_not_fail_deletion(self):
        """WebSocket emission errors must not fail the deletion."""
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        request = AssetBulkDeleteDto(ids=[uuid4()], force=False)
        current_user_id = uuid4()

        # Patch the underlying emit so the SocketIOError originates *inside*
        # emit_user_event (which now swallows it centrally).
        with patch(
            "services.websockets._emit_event",
            new_callable=AsyncMock,
            side_effect=SocketIOError("WebSocket error"),
        ):
            result = await delete_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert result.status_code == 204

    @pytest.mark.anyio
    async def test_delete_assets_empty_id_list_is_noop(self):
        """An empty ids list returns 204 without calling the backend."""
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)
        mock_client.delete = AsyncMock(return_value=None)

        request = AssetBulkDeleteDto(ids=[], force=False)

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            result = await delete_assets(
                request, client=mock_client, current_user_id=uuid4()
            )

        assert result.status_code == 204
        mock_client.post.assert_not_awaited()
        mock_client.delete.assert_not_awaited()

    @pytest.mark.anyio
    async def test_delete_assets_force_false_propagates_sdk_error(self):
        """SDK errors on bulk-trash bubble to the global GumnutError handler.

        Pins the no-swallow contract: a future refactor that wraps client.post
        in try/except (e.g. to ignore per-id 404s the way the legacy per-id
        loop did) must break this test.
        """
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.post = AsyncMock(side_effect=make_sdk_status_error(500, "boom"))

        request = AssetBulkDeleteDto(ids=[uuid4()], force=False)

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            with pytest.raises(APIStatusError):
                await delete_assets(
                    request, client=mock_client, current_user_id=uuid4()
                )

    @pytest.mark.anyio
    async def test_delete_assets_force_true_propagates_sdk_error(self):
        """SDK errors on bulk hard-delete bubble to the global GumnutError handler."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.delete = AsyncMock(side_effect=make_sdk_status_error(500, "boom"))

        request = AssetBulkDeleteDto(ids=[uuid4()], force=True)

        with patch("routers.api.assets.emit_user_event", new_callable=AsyncMock):
            with pytest.raises(APIStatusError):
                await delete_assets(
                    request, client=mock_client, current_user_id=uuid4()
                )


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

    @pytest.mark.anyio
    async def test_get_asset_statistics_is_trashed_passes_state(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """isTrashed=True routes to assets.list(state='trashed')."""
        mock_client = Mock()

        assets = multiple_gumnut_assets
        assets[0].mime_type = "image/jpeg"
        assets[1].mime_type = "video/mp4"
        assets[2].mime_type = "image/png"

        mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

        result = await get_asset_statistics(isTrashed=True, client=mock_client)

        assert result.total == 3
        assert result.images == 2
        assert result.videos == 1
        mock_client.assets.list.assert_called_once_with(state="trashed")

    @pytest.mark.anyio
    async def test_get_asset_statistics_is_trashed_false_omits_state(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """isTrashed=False (or absent) calls assets.list() with no state — backend default (live) applies."""
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page(
            multiple_gumnut_assets
        )

        await get_asset_statistics(isTrashed=False, client=mock_client)

        mock_client.assets.list.assert_called_once_with()


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


def _make_mock_asset_with_urls(
    variant_map: dict[str, dict[str, str]],
    mime_type: str = "image/jpeg",
    width: int | None = None,
    height: int | None = None,
):
    """Create a mock asset with asset_urls (Mock objects with .url/.mimetype attrs).

    `width`/`height` default to None so the aspect-ratio variant upgrade
    (`_upgrade_variant_for_aspect`) falls back to the requested variant — set
    them explicitly to exercise the wide-landscape upgrade path.
    """
    asset = Mock()
    asset.mime_type = mime_type
    asset.width = width
    asset.height = height
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

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("requested_size", "expected_key"),
        [
            (AssetMediaSize.thumbnail, "thumbnail_image"),
            (AssetMediaSize.preview, "preview_image"),
            (AssetMediaSize.fullsize, "fullsize_image"),
        ],
    )
    async def test_view_asset_video_resolves_image_suffixed_variant(
        self, sample_uuid, requested_size, expected_key
    ):
        """Video assets resolve still-image variants to `_image`-suffixed keys."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    expected_key: {
                        "url": f"https://cdn.example.com/{expected_key}.webp",
                        "mimetype": "image/webp",
                    }
                },
                mime_type="video/mp4",
            )
        )

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = Mock()
            await view_asset(sample_uuid, size=requested_size, client=mock_client)

        mock_cdn.assert_called_once_with(
            f"https://cdn.example.com/{expected_key}.webp",
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
    async def test_view_asset_video_thumbnail_image_missing_returns_404(
        self, sample_uuid
    ):
        """Pre-extraction videos (no `_image` variants) return 404."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "original": {
                        "url": "https://cdn.example.com/clip.mp4",
                        "mimetype": "video/mp4",
                    }
                },
                mime_type="video/mp4",
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            await view_asset(
                sample_uuid, size=AssetMediaSize.thumbnail, client=mock_client
            )

        assert exc_info.value.status_code == 404
        assert "thumbnail_image" in exc_info.value.detail

    @pytest.mark.anyio
    async def test_view_asset_wide_landscape_thumbnail_upgrades_to_small(
        self, sample_uuid
    ):
        """A wide-landscape (wider than 2:1) image thumbnail request streams the
        small variant."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "thumbnail": {
                        "url": "https://cdn.example.com/thumb.webp",
                        "mimetype": "image/webp",
                    },
                    "small": {
                        "url": "https://cdn.example.com/small.jpg",
                        "mimetype": "image/jpeg",
                    },
                    "preview": {
                        "url": "https://cdn.example.com/preview.jpg",
                        "mimetype": "image/jpeg",
                    },
                },
                width=2400,  # ratio 2.4 (> 2)
                height=1000,
            )
        )

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = Mock()
            await view_asset(
                sample_uuid, size=AssetMediaSize.thumbnail, client=mock_client
            )

        # The 720px small (JPEG) is streamed in place of the 360px thumbnail —
        # not the heavier 1440px preview, even though it is also available.
        mock_cdn.assert_called_once_with(
            "https://cdn.example.com/small.jpg",
            "image/jpeg",
            range_header=None,
            forwarded_headers=(
                "content-length",
                "etag",
                "last-modified",
                "cache-control",
            ),
        )

    @pytest.mark.anyio
    async def test_view_asset_wide_landscape_video_upgrades_to_small_image(
        self, sample_uuid
    ):
        """A wide-landscape video thumbnail request streams the small_image."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "thumbnail_image": {
                        "url": "https://cdn.example.com/thumb_image.webp",
                        "mimetype": "image/webp",
                    },
                    "small_image": {
                        "url": "https://cdn.example.com/small_image.jpg",
                        "mimetype": "image/jpeg",
                    },
                    "preview_image": {
                        "url": "https://cdn.example.com/preview_image.jpg",
                        "mimetype": "image/jpeg",
                    },
                },
                mime_type="video/mp4",
                width=2400,  # ratio 2.4 (> 2)
                height=1000,
            )
        )

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = Mock()
            await view_asset(
                sample_uuid, size=AssetMediaSize.thumbnail, client=mock_client
            )

        mock_cdn.assert_called_once_with(
            "https://cdn.example.com/small_image.jpg",
            "image/jpeg",
            range_header=None,
            forwarded_headers=(
                "content-length",
                "etag",
                "last-modified",
                "cache-control",
            ),
        )

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("mime_type", "present_key", "expected_missing_key"),
        [
            ("image/jpeg", "thumbnail", "small"),
            ("video/mp4", "thumbnail_image", "small_image"),
        ],
    )
    async def test_view_asset_wide_landscape_missing_small_returns_404(
        self, sample_uuid, mime_type, present_key, expected_missing_key
    ):
        """The aspect upgrade rewrites the variant *before* the asset_urls
        existence check, so a wide-landscape thumbnail request 404s on the
        upgraded key (`small` / `small_image`) — not the present `thumbnail` —
        rather than falling back, if the backend ever stops emitting it."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    present_key: {
                        "url": "https://cdn.example.com/thumb.webp",
                        "mimetype": "image/webp",
                    },
                },
                mime_type=mime_type,
                width=2400,  # ratio 2.4 (> 2)
                height=1000,
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            await view_asset(
                sample_uuid, size=AssetMediaSize.thumbnail, client=mock_client
            )

        assert exc_info.value.status_code == 404
        assert expected_missing_key in exc_info.value.detail

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("width", "height"),
        [
            (1080, 1920),  # portrait 9:16 — tall, width <= height
            (1000, 1000),  # square
            (1200, 1000),  # landscape 6:5, ratio 1.2 (<= 2)
            (1500, 1000),  # landscape 3:2, ratio 1.5 (<= 2)
            (1920, 1080),  # landscape 16:9, ratio ~1.78 (<= 2) — mild upscale is fine
            (2000, 1000),  # landscape 2:1, ratio 2.0 (boundary, not > 2)
            (None, None),  # dimensions unknown
            (0, 0),  # photos-api "unknown" sentinel
        ],
    )
    async def test_view_asset_non_wide_landscape_thumbnail_stays_thumbnail(
        self, sample_uuid, width, height
    ):
        """Portrait, square, 16:9, the 2:1 boundary, and unknown-dim assets keep
        the cheap 360px thumbnail."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "thumbnail": {
                        "url": "https://cdn.example.com/thumb.webp",
                        "mimetype": "image/webp",
                    },
                    "small": {
                        "url": "https://cdn.example.com/small.jpg",
                        "mimetype": "image/jpeg",
                    },
                },
                width=width,
                height=height,
            )
        )

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = Mock()
            await view_asset(
                sample_uuid, size=AssetMediaSize.thumbnail, client=mock_client
            )

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
    async def test_view_asset_wide_landscape_preview_request_unaffected(
        self, sample_uuid
    ):
        """A `preview`-size request on a wide-landscape asset is not re-upgraded
        (only `thumbnail` requests get the aspect-based upgrade)."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "preview": {
                        "url": "https://cdn.example.com/preview.jpg",
                        "mimetype": "image/jpeg",
                    },
                },
                width=2400,  # ratio 2.4 (> 2) — would upgrade if this were a thumbnail request
                height=1000,
            )
        )

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = Mock()
            await view_asset(
                sample_uuid, size=AssetMediaSize.preview, client=mock_client
            )

        mock_cdn.assert_called_once_with(
            "https://cdn.example.com/preview.jpg",
            "image/jpeg",
            range_header=None,
            forwarded_headers=(
                "content-length",
                "etag",
                "last-modified",
                "cache-control",
            ),
        )


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


class TestPlayAssetVideo:
    """Test the play_asset_video endpoint."""

    @pytest.mark.anyio
    async def test_play_asset_video_success(self, sample_uuid):
        """Streams the original variant from CDN with no Range header."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "original": {
                        "url": "https://cdn.example.com/video.mp4",
                        "mimetype": "video/mp4",
                    }
                },
                mime_type="video/mp4",
            )
        )
        mock_streaming_response = Mock()
        mock_request = Mock()
        mock_request.headers = {}

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = mock_streaming_response
            result = await play_asset_video(
                sample_uuid, request=mock_request, client=mock_client
            )

        assert result is mock_streaming_response
        mock_cdn.assert_called_once_with(
            "https://cdn.example.com/video.mp4",
            "video/mp4",
            range_header=None,
            forwarded_headers=(
                "content-length",
                "etag",
                "last-modified",
                "cache-control",
            ),
        )

    @pytest.mark.anyio
    async def test_play_asset_video_forwards_range_header(self, sample_uuid):
        """The client's Range header is forwarded to stream_from_cdn for seeking."""
        mock_client = Mock()
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_mock_asset_with_urls(
                {
                    "original": {
                        "url": "https://cdn.example.com/video.mp4",
                        "mimetype": "video/mp4",
                    }
                },
                mime_type="video/mp4",
            )
        )
        mock_request = Mock()
        mock_request.headers = {"range": "bytes=1000-2000"}

        with patch(
            "routers.api.assets.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = Mock()
            await play_asset_video(
                sample_uuid, request=mock_request, client=mock_client
            )

        mock_cdn.assert_called_once_with(
            "https://cdn.example.com/video.mp4",
            "video/mp4",
            range_header="bytes=1000-2000",
            forwarded_headers=(
                "content-length",
                "etag",
                "last-modified",
                "cache-control",
            ),
        )

    @pytest.mark.anyio
    async def test_play_asset_video_missing_original_variant(self, sample_uuid):
        """A 404 is raised when the asset has no `original` variant URL."""
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
        mock_request = Mock()
        mock_request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            await play_asset_video(
                sample_uuid, request=mock_request, client=mock_client
            )

        assert exc_info.value.status_code == 404


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
