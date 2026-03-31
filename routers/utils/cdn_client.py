"""
CDN HTTP client for fetching asset bytes from signed CDN URLs.

Provides a singleton async httpx client (no auth headers, no response hooks)
and a streaming helper that maps CDN errors to adapter HTTP exceptions.
"""

import asyncio
import logging

import httpx
from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

_cdn_http_client: httpx.AsyncClient | None = None
_cdn_client_lock = asyncio.Lock()


async def get_cdn_http_client() -> httpx.AsyncClient:
    """Get or create the singleton async HTTP client for CDN fetches.

    No auth headers or response hooks — CDN URLs are pre-signed.
    """
    global _cdn_http_client
    if _cdn_http_client is None:
        async with _cdn_client_lock:
            if _cdn_http_client is None:
                # read=120: per-chunk timeout, not total transfer time. A large
                # file actively streaming will never hit it — it only fires if
                # the CDN goes silent for 2 minutes, preventing hung connections.
                _cdn_http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=10.0, read=120.0, write=30.0, pool=30.0
                    ),
                    limits=httpx.Limits(
                        max_connections=100, max_keepalive_connections=20
                    ),
                    follow_redirects=True,
                    trust_env=False,
                )
    return _cdn_http_client


async def close_cdn_http_client() -> None:
    """Close the singleton CDN HTTP client. Call on application shutdown."""
    global _cdn_http_client
    if _cdn_http_client is not None:
        await _cdn_http_client.aclose()
        _cdn_http_client = None


DEFAULT_FORWARDED_HEADERS = (
    "content-length",
    "etag",
    "last-modified",
    "cache-control",
)


async def stream_from_cdn(
    cdn_url: str,
    mimetype: str,
    range_header: str | None = None,
    forwarded_headers: tuple[str, ...] = DEFAULT_FORWARDED_HEADERS,
) -> StreamingResponse:
    """Stream asset bytes from a signed CDN URL.

    Args:
        cdn_url: Pre-signed CDN URL for the asset variant.
        mimetype: Fallback MIME type if CDN response lacks Content-Type.
        range_header: Optional Range header value to forward for video seeking.
        forwarded_headers: Upstream headers to forward. Defaults exclude
            content-disposition; callers that need it (e.g. /original download)
            should pass ``DEFAULT_FORWARDED_HEADERS + ("content-disposition",)``.

    Returns:
        StreamingResponse that streams CDN bytes to the Immich client.

    Raises:
        HTTPException: 404 for CDN 403/404, 416 for range-not-satisfiable,
            502 for CDN 5xx or connection errors.
    """
    client = await get_cdn_http_client()

    headers: dict[str, str] = {}
    if range_header is not None:
        headers["Range"] = range_header

    try:
        cdn_response = await client.send(
            client.build_request("GET", cdn_url, headers=headers),
            stream=True,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "CDN connection error", extra={"cdn_url": cdn_url, "error": str(exc)}
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch asset from CDN",
        ) from exc

    if cdn_response.status_code in (403, 404):
        logger.warning(
            "CDN asset not found",
            extra={"cdn_url": cdn_url, "status_code": cdn_response.status_code},
        )
        await cdn_response.aclose()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asset not found",
        )

    if cdn_response.status_code == 416:
        logger.warning("CDN range not satisfiable", extra={"cdn_url": cdn_url})
        error_headers: dict[str, str] = {"Accept-Ranges": "bytes"}
        if cr := cdn_response.headers.get("content-range"):
            error_headers["Content-Range"] = cr
        await cdn_response.aclose()
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="Requested range not satisfiable",
            headers=error_headers,
        )

    if cdn_response.status_code >= 400:
        logger.warning(
            "CDN upstream error",
            extra={"cdn_url": cdn_url, "status_code": cdn_response.status_code},
        )
        await cdn_response.aclose()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="CDN upstream error",
        )

    content_type = cdn_response.headers.get("content-type") or mimetype
    response_headers: dict[str, str] = {}

    # Forward allowlisted upstream headers when present
    for h in forwarded_headers:
        v = cdn_response.headers.get(h)
        if v:
            response_headers[h if h == "etag" else h.title()] = v

    if cdn_response.status_code == 206:
        content_range = cdn_response.headers.get("content-range")
        if content_range:
            response_headers["Content-Range"] = content_range
        response_headers["Accept-Ranges"] = "bytes"

    async def _stream_and_close():
        try:
            async for chunk in cdn_response.aiter_bytes(chunk_size=8192):
                yield chunk
        finally:
            await cdn_response.aclose()

    return StreamingResponse(
        _stream_and_close(),
        status_code=cdn_response.status_code,
        media_type=content_type,
        headers=response_headers,
    )
