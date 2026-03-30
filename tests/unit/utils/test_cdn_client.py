"""Tests for cdn_client.py CDN streaming helper."""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import HTTPException

from routers.utils.cdn_client import stream_from_cdn


@pytest.fixture
def mock_cdn_response():
    """Create a mock httpx response for CDN fetches."""

    def _create(status_code=200, headers=None):
        response = Mock()
        response.status_code = status_code
        response.headers = headers or {}
        response.aclose = AsyncMock()

        async def _aiter_bytes(chunk_size=None):
            yield b"fake cdn data"

        response.aiter_bytes = _aiter_bytes
        return response

    return _create


class TestStreamFromCdn:
    """Test the stream_from_cdn helper."""

    @pytest.mark.anyio
    async def test_success(self, mock_cdn_response):
        """Test successful CDN streaming."""
        cdn_response = mock_cdn_response(200)
        mock_client = AsyncMock()
        mock_client.build_request.return_value = Mock()
        mock_client.send = AsyncMock(return_value=cdn_response)

        with patch(
            "routers.utils.cdn_client.get_cdn_http_client",
            new_callable=AsyncMock,
            return_value=mock_client,
        ):
            result = await stream_from_cdn(
                "https://cdn.example.com/asset.jpg", "image/jpeg"
            )

        assert result.status_code == 200
        assert result.media_type == "image/jpeg"

    @pytest.mark.anyio
    async def test_range_request_206(self, mock_cdn_response):
        """Test range request forwarding returns 206."""
        cdn_response = mock_cdn_response(
            206,
            headers={
                "content-range": "bytes 0-999/5000",
                "content-length": "1000",
            },
        )
        mock_client = AsyncMock()
        mock_client.build_request.return_value = Mock()
        mock_client.send = AsyncMock(return_value=cdn_response)

        with patch(
            "routers.utils.cdn_client.get_cdn_http_client",
            new_callable=AsyncMock,
            return_value=mock_client,
        ):
            result = await stream_from_cdn(
                "https://cdn.example.com/video.mp4",
                "video/mp4",
                range_header="bytes=0-999",
            )

        assert result.status_code == 206
        assert result.headers["Content-Range"] == "bytes 0-999/5000"
        assert result.headers["Accept-Ranges"] == "bytes"
        assert result.headers["Content-Length"] == "1000"

        # Verify Range header was included in the request
        mock_client.build_request.assert_called_once()
        call_kwargs = mock_client.build_request.call_args
        assert call_kwargs[1]["headers"]["Range"] == "bytes=0-999"

    @pytest.mark.anyio
    async def test_cdn_403_maps_to_404(self, mock_cdn_response):
        """Test CDN 403 is mapped to adapter 404."""
        cdn_response = mock_cdn_response(403)
        mock_client = AsyncMock()
        mock_client.build_request.return_value = Mock()
        mock_client.send = AsyncMock(return_value=cdn_response)

        with patch(
            "routers.utils.cdn_client.get_cdn_http_client",
            new_callable=AsyncMock,
            return_value=mock_client,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await stream_from_cdn("https://cdn.example.com/asset.jpg", "image/jpeg")

        assert exc_info.value.status_code == 404
        cdn_response.aclose.assert_called_once()

    @pytest.mark.anyio
    async def test_cdn_404_maps_to_404(self, mock_cdn_response):
        """Test CDN 404 is mapped to adapter 404."""
        cdn_response = mock_cdn_response(404)
        mock_client = AsyncMock()
        mock_client.build_request.return_value = Mock()
        mock_client.send = AsyncMock(return_value=cdn_response)

        with patch(
            "routers.utils.cdn_client.get_cdn_http_client",
            new_callable=AsyncMock,
            return_value=mock_client,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await stream_from_cdn("https://cdn.example.com/asset.jpg", "image/jpeg")

        assert exc_info.value.status_code == 404
        cdn_response.aclose.assert_called_once()

    @pytest.mark.anyio
    async def test_cdn_502_maps_to_502(self, mock_cdn_response):
        """Test CDN 5xx is mapped to adapter 502."""
        cdn_response = mock_cdn_response(502)
        mock_client = AsyncMock()
        mock_client.build_request.return_value = Mock()
        mock_client.send = AsyncMock(return_value=cdn_response)

        with patch(
            "routers.utils.cdn_client.get_cdn_http_client",
            new_callable=AsyncMock,
            return_value=mock_client,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await stream_from_cdn("https://cdn.example.com/asset.jpg", "image/jpeg")

        assert exc_info.value.status_code == 502
        cdn_response.aclose.assert_called_once()

    @pytest.mark.anyio
    async def test_cdn_429_maps_to_502(self, mock_cdn_response):
        """Test CDN 429 is mapped to adapter 502 (never expose 429 to Immich clients)."""
        cdn_response = mock_cdn_response(429)
        mock_client = AsyncMock()
        mock_client.build_request.return_value = Mock()
        mock_client.send = AsyncMock(return_value=cdn_response)

        with patch(
            "routers.utils.cdn_client.get_cdn_http_client",
            new_callable=AsyncMock,
            return_value=mock_client,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await stream_from_cdn("https://cdn.example.com/asset.jpg", "image/jpeg")

        assert exc_info.value.status_code == 502
        cdn_response.aclose.assert_called_once()

    @pytest.mark.anyio
    async def test_cdn_connection_error_maps_to_502(self):
        """Test CDN connection errors are mapped to adapter 502."""
        import httpx

        mock_client = AsyncMock()
        mock_client.build_request.return_value = Mock()
        mock_client.send = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        with patch(
            "routers.utils.cdn_client.get_cdn_http_client",
            new_callable=AsyncMock,
            return_value=mock_client,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await stream_from_cdn("https://cdn.example.com/asset.jpg", "image/jpeg")

        assert exc_info.value.status_code == 502
