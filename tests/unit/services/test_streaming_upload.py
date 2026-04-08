"""Tests for the StreamingUploadPipeline."""

from datetime import datetime
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from services.streaming_upload import StreamingUploadPipeline


# Minimal UploadFields stand-in (mirrors the real NamedTuple in assets.py)
class _UploadFields(NamedTuple):
    device_asset_id: str
    device_id: str
    file_created_at: datetime
    file_modified_at: datetime


def _extract_fields(fields: dict[str, str]) -> _UploadFields:
    return _UploadFields(
        device_asset_id=fields.get("deviceAssetId", ""),
        device_id=fields.get("deviceId", ""),
        file_created_at=datetime(2023, 1, 1),
        file_modified_at=datetime(2023, 1, 1),
    )


_REQUIRED_FIELDS = {
    "deviceAssetId": "dev-123",
    "deviceId": "dev-456",
    "fileCreatedAt": "2023-01-01T00:00:00Z",
}

_BOUNDARY = "----TestBoundary123"


def _build_multipart_body(
    fields: dict[str, str] | None = None,
    file_data: bytes = b"fake image data",
    filename: str = "test.jpg",
    content_type: str = "image/jpeg",
) -> tuple[bytes, str]:
    """Build a multipart/form-data body for testing."""
    if fields is None:
        fields = _REQUIRED_FIELDS
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{_BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            f"\r\n"
            f"{value}\r\n"
        )
    parts.append(
        f"--{_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="assetData"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n"
        f"\r\n"
    )
    body = "".join(parts).encode() + file_data + f"\r\n--{_BOUNDARY}--\r\n".encode()
    ct_header = f"multipart/form-data; boundary={_BOUNDARY}"
    return body, ct_header


def _make_mock_request(body: bytes, content_type: str) -> MagicMock:
    """Create a mock Request with an async stream yielding the body."""
    request = MagicMock()
    request.headers = {"content-type": content_type}

    async def stream():
        # Yield in chunks to exercise the pipeline
        chunk_size = 256
        for i in range(0, len(body), chunk_size):
            yield body[i : i + chunk_size]

    request.stream = stream
    return request


def _make_httpx_response(
    status_code: int = 201,
    json_data: dict | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    """Create a real httpx.Response with the given status and JSON body."""
    if json_data is None:
        json_data = {"id": "asset_abc123", "status": "created"}
    import json

    response = httpx.Response(
        status_code=status_code,
        content=json.dumps(json_data).encode(),
        headers={"content-type": "application/json", **(headers or {})},
    )
    return response


class TestStreamingUploadPipeline:
    @pytest.mark.anyio
    async def test_successful_upload(self):
        """Test the full pipeline: feed → parse → upload → result."""
        body, ct_header = _build_multipart_body()
        request = _make_mock_request(body, ct_header)
        base_url = "http://localhost:8000"
        response = _make_httpx_response(
            201, {"id": "asset_abc123", "status": "created"}
        )

        mock_client = MagicMock()
        mock_client.post.return_value = response

        with patch(
            "services.streaming_upload._get_streaming_http_client",
            return_value=mock_client,
        ):
            pipeline = StreamingUploadPipeline(request, base_url, "test-jwt")
            result = await pipeline.execute(_extract_fields)

        assert result["id"] == "asset_abc123"
        assert result["status"] == "created"
        assert result["_http_status"] == 201
        mock_client.post.assert_called_once()

    @pytest.mark.anyio
    async def test_5xx_maps_to_502(self):
        """Test that upstream 5xx is mapped to 502."""
        body, ct_header = _build_multipart_body()
        request = _make_mock_request(body, ct_header)
        base_url = "http://localhost:8000"
        response = _make_httpx_response(500, {"detail": "Internal server error"})

        mock_client = MagicMock()
        mock_client.post.return_value = response

        with patch(
            "services.streaming_upload._get_streaming_http_client",
            return_value=mock_client,
        ):
            pipeline = StreamingUploadPipeline(request, base_url, "test-jwt")
            with pytest.raises(HTTPException) as exc_info:
                await pipeline.execute(_extract_fields)

        assert exc_info.value.status_code == 502

    @pytest.mark.anyio
    async def test_429_maps_to_502(self):
        """Test that upstream 429 is mapped to 502."""
        body, ct_header = _build_multipart_body()
        request = _make_mock_request(body, ct_header)
        base_url = "http://localhost:8000"
        response = _make_httpx_response(429, {"detail": "Rate limited"})

        mock_client = MagicMock()
        mock_client.post.return_value = response

        with patch(
            "services.streaming_upload._get_streaming_http_client",
            return_value=mock_client,
        ):
            pipeline = StreamingUploadPipeline(request, base_url, "test-jwt")
            with pytest.raises(HTTPException) as exc_info:
                await pipeline.execute(_extract_fields)

        assert exc_info.value.status_code == 502

    @pytest.mark.anyio
    async def test_refreshed_token_captured(self):
        """Test that x-new-access-token from photos-api is captured."""
        body, ct_header = _build_multipart_body()
        request = _make_mock_request(body, ct_header)
        base_url = "http://localhost:8000"
        response = _make_httpx_response(
            201,
            {"id": "asset_abc123", "status": "created"},
            headers={"x-new-access-token": "new-jwt-token"},
        )

        mock_client = MagicMock()
        mock_client.post.return_value = response

        with (
            patch(
                "services.streaming_upload._get_streaming_http_client",
                return_value=mock_client,
            ),
            patch("services.streaming_upload.set_refreshed_token") as mock_set,
        ):
            pipeline = StreamingUploadPipeline(request, base_url, "test-jwt")
            await pipeline.execute(_extract_fields)

        assert pipeline.refreshed_token == "new-jwt-token"
        mock_set.assert_called_once_with("new-jwt-token")

    @pytest.mark.anyio
    async def test_duplicate_response(self):
        """Test that a duplicate response is returned correctly."""
        body, ct_header = _build_multipart_body()
        request = _make_mock_request(body, ct_header)
        base_url = "http://localhost:8000"
        response = _make_httpx_response(
            200, {"id": "asset_existing", "status": "duplicate"}
        )

        mock_client = MagicMock()
        mock_client.post.return_value = response

        with patch(
            "services.streaming_upload._get_streaming_http_client",
            return_value=mock_client,
        ):
            pipeline = StreamingUploadPipeline(request, base_url, "test-jwt")
            result = await pipeline.execute(_extract_fields)

        assert result["status"] == "duplicate"
        assert result["_http_status"] == 200

    @pytest.mark.anyio
    async def test_4xx_forwarded_as_is(self):
        """Test that 4xx errors from photos-api are forwarded with their status code."""
        body, ct_header = _build_multipart_body()
        request = _make_mock_request(body, ct_header)
        base_url = "http://localhost:8000"
        response = _make_httpx_response(413, {"detail": "Too large"})

        mock_client = MagicMock()
        mock_client.post.return_value = response

        with patch(
            "services.streaming_upload._get_streaming_http_client",
            return_value=mock_client,
        ):
            pipeline = StreamingUploadPipeline(request, base_url, "test-jwt")
            with pytest.raises(HTTPException) as exc_info:
                await pipeline.execute(_extract_fields)

        # 4xx (other than 401) should be forwarded as-is, not mapped to 502
        assert exc_info.value.status_code == 413

    @pytest.mark.anyio
    async def test_401_mapped_to_502(self):
        """Test that 401 from photos-api maps to 502 (adapter's JWT expired, not client's session)."""
        body, ct_header = _build_multipart_body()
        request = _make_mock_request(body, ct_header)
        base_url = "http://localhost:8000"
        response = _make_httpx_response(401, {"detail": "Unauthorized"})

        mock_client = MagicMock()
        mock_client.post.return_value = response

        with patch(
            "services.streaming_upload._get_streaming_http_client",
            return_value=mock_client,
        ):
            pipeline = StreamingUploadPipeline(request, base_url, "test-jwt")
            with pytest.raises(HTTPException) as exc_info:
                await pipeline.execute(_extract_fields)

        # 401 from photos-api is an internal auth issue, not the client's
        assert exc_info.value.status_code == 502
