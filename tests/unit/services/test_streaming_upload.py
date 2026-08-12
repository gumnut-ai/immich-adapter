"""Tests for the StreamingUploadPipeline."""

import asyncio
import gc
from datetime import datetime
from typing import NamedTuple
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from services.streaming_upload import StreamingUploadPipeline


# Minimal UploadFields stand-in (mirrors the real NamedTuple in assets.py).
# Immich v3 dropped the device fields; the pipeline synthesizes them internally.
class _UploadFields(NamedTuple):
    file_created_at: datetime
    file_modified_at: datetime


def _extract_fields(fields: dict[str, str]) -> _UploadFields:
    return _UploadFields(
        file_created_at=datetime(2023, 1, 1),
        file_modified_at=datetime(2023, 1, 1),
    )


_REQUIRED_FIELDS = {
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
        assert pipeline.last_status_code == 201
        mock_client.post.assert_called_once()

    @pytest.mark.anyio
    async def test_synthesizes_device_fields_for_gumnut(self):
        """Immich v3 sends no device fields; the pipeline synthesizes what the
        Gumnut API requires — a unique per-upload device_asset_id and a
        placeholder device_id — and forwards them in the upload body."""
        body, ct_header = _build_multipart_body(filename="photo.jpg")
        request = _make_mock_request(body, ct_header)
        response = _make_httpx_response(201)

        mock_client = MagicMock()
        mock_client.post.return_value = response

        with patch(
            "services.streaming_upload._get_streaming_http_client",
            return_value=mock_client,
        ):
            pipeline = StreamingUploadPipeline(request, "http://localhost:8000", "jwt")
            await pipeline.execute(_extract_fields)

        sent_data = mock_client.post.call_args.kwargs["data"]
        # A unique UUID so distinct assets never collapse onto one device tuple.
        UUID(sent_data["device_asset_id"])  # raises if not a valid UUID
        assert sent_data["device_id"] == "gumnut-device"

    @pytest.mark.anyio
    async def test_device_asset_id_unique_per_upload(self):
        """Each upload gets a fresh device_asset_id — the whole reason it is
        synthesized per-upload rather than shared. Guards against a regression
        that hoisted the UUID to module scope or reused GUMNUT_UPLOAD_DEVICE_ID,
        which would still parse as a valid UUID but collapse distinct assets."""
        mock_client = MagicMock()
        mock_client.post.return_value = _make_httpx_response(201)

        device_asset_ids = []
        with patch(
            "services.streaming_upload._get_streaming_http_client",
            return_value=mock_client,
        ):
            for _ in range(2):
                body, ct_header = _build_multipart_body()
                request = _make_mock_request(body, ct_header)
                pipeline = StreamingUploadPipeline(
                    request, "http://localhost:8000", "jwt"
                )
                await pipeline.execute(_extract_fields)
                device_asset_ids.append(
                    mock_client.post.call_args.kwargs["data"]["device_asset_id"]
                )

        assert device_asset_ids[0] != device_asset_ids[1]

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
    async def test_507_maps_to_400_quota_exceeded(self):
        """Test that an over-quota upload (upstream 507) maps to Immich's 400."""
        body, ct_header = _build_multipart_body()
        request = _make_mock_request(body, ct_header)
        base_url = "http://localhost:8000"
        response = _make_httpx_response(507, {"detail": "User storage limit exceeded"})

        mock_client = MagicMock()
        mock_client.post.return_value = response

        with patch(
            "services.streaming_upload._get_streaming_http_client",
            return_value=mock_client,
        ):
            pipeline = StreamingUploadPipeline(request, base_url, "test-jwt")
            with pytest.raises(HTTPException) as exc_info:
                await pipeline.execute(_extract_fields)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Quota has been exceeded!"

    @pytest.mark.anyio
    async def test_refreshed_token_captured(self):
        """Test that x-new-access-token from the Gumnut API is captured."""
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
        assert pipeline.last_status_code == 200

    @pytest.mark.anyio
    async def test_4xx_forwarded_as_is(self):
        """Test that 4xx errors from the Gumnut API are forwarded with their status code."""
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
    async def test_feeder_disconnect_exception_is_retrieved(self):
        """A mid-body client disconnect must not leave the feeder task's
        exception unretrieved — an unretrieved task exception is logged by
        asyncio at error level ("Task exception was never retrieved") when the
        task is garbage-collected, turning every aborted upload into error
        noise even though the route already answers it as an expected 499."""
        _, ct_header = _build_multipart_body()

        request = MagicMock()
        request.headers = {"content-type": ct_header}

        async def stream():
            # The client hangs up before any body bytes arrive. Failing on the
            # first read makes the feeder finish (with this exception) before
            # the upload thread can observe the error, which is the ordering
            # that leaves the task's exception unretrieved.
            raise ClientDisconnect()
            yield b""  # pragma: no cover — makes this an async generator

        request.stream = stream

        # The upload never reaches the HTTP POST: the parser fails on the
        # missing body first.
        mock_client = MagicMock()

        captured: list[dict] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: captured.append(context))
        try:
            with patch(
                "services.streaming_upload._get_streaming_http_client",
                return_value=mock_client,
            ):
                pipeline = StreamingUploadPipeline(
                    request, "http://localhost:8000", "jwt"
                )
                # Plain try/except instead of pytest.raises: the excinfo would
                # keep the traceback (and through it the feeder task) alive,
                # letting the task dodge the garbage collection this test
                # depends on.
                raised = False
                try:
                    await pipeline.execute(_extract_fields)
                except ClientDisconnect:
                    raised = True
                assert raised, "expected the disconnect to fail the upload"

            del pipeline
            # The task only becomes collectable after the loop finishes the
            # turn that delivered the failure (lingering frame/exception
            # references), so give it a few turns before collecting.
            for _ in range(4):
                await asyncio.sleep(0)
                gc.collect()
        finally:
            loop.set_exception_handler(previous_handler)

        unretrieved = [
            context
            for context in captured
            if "never retrieved" in context.get("message", "")
        ]
        assert unretrieved == []

    @pytest.mark.anyio
    async def test_401_mapped_to_502(self):
        """Test that 401 from the Gumnut API maps to 502 (adapter's JWT expired, not client's session)."""
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

        # 401 from the Gumnut API is an internal auth issue, not the client's
        assert exc_info.value.status_code == 502
