"""HTTP-level video playback through auth, the route, and the real CDN proxy."""

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gumnut import NotFoundError

from config.exceptions import configure_exception_handlers
from routers.api.assets import router
from routers.middleware.auth_middleware import AuthMiddleware
from routers.utils.gumnut_client import (
    LibraryScope,
    bind_library_scope,
    get_authenticated_gumnut_client,
)
from services.thumbnail_cache import ThumbnailCache
from services.video_cache import is_cacheable_video_selection
from tests.conftest import make_sdk_status_error

CDN_URL = "https://assets.gumnut.ai/version/original?verify=" + "a" * 43 + "&dl=a"


def _asset(mimetype: str) -> Mock:
    metadata = Mock(width=100, height=100, mime_type=mimetype, thumbhash=None)
    metadata.asset_urls = {
        "original": SimpleNamespace(url=CDN_URL, mimetype=mimetype),
    }
    return metadata


@pytest.fixture
def playback():
    """Yield (http, sdk, store, cdn_requests) with the CDN served by a mock transport."""
    sdk = Mock()
    sdk.assets.retrieve = AsyncMock(return_value=_asset("video/mp4"))
    claims = base64.urlsafe_b64encode(
        json.dumps({"exp": time.time() + 120}).encode()
    ).decode()
    session = Mock(
        user_id=str(uuid4()),
        library_id="library",
        library_checked_at=time.time(),
        library_from_choice=False,
    )
    session.get_jwt.return_value = "header." + claims + ".signature"
    store = AsyncMock()
    store.get_by_id.return_value = session
    cache = ThumbnailCache(
        5, 10, namespace="video", cacheable=is_cacheable_video_selection
    )
    cdn_requests: list[httpx.Request] = []

    def cdn(request: httpx.Request) -> httpx.Response:
        cdn_requests.append(request)
        return httpx.Response(
            206,
            content=b"0123",
            headers={
                "content-type": "video/mp4",
                "content-range": "bytes 0-3/100",
                "content-length": "4",
                "content-disposition": "attachment; filename*=UTF-8''clip.mp4",
            },
        )

    cdn_client = httpx.AsyncClient(transport=httpx.MockTransport(cdn))
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    configure_exception_handlers(app)
    app.include_router(router)

    async def authenticated():
        bind_library_scope(LibraryScope("library", AsyncMock()))
        return sdk

    app.dependency_overrides[get_authenticated_gumnut_client] = authenticated
    with (
        patch(
            "routers.middleware.auth_middleware.get_session_store",
            AsyncMock(return_value=store),
        ),
        patch("routers.middleware.auth_middleware.get_video_cache", return_value=cache),
        patch("routers.api.assets.get_video_cache", return_value=cache),
        patch(
            "routers.utils.thumbnail_cache.get_settings",
            return_value=SimpleNamespace(gumnut_api_base_url="https://api.example.com"),
        ),
        patch(
            "routers.utils.cdn_client.get_cdn_http_client",
            AsyncMock(return_value=cdn_client),
        ),
        TestClient(app) as http,
    ):
        yield http, sdk, store, cdn_requests


HEADERS = {"authorization": "Bearer session-token", "range": "bytes=0-3"}


def test_video_streams_inline_with_range(playback):
    http, _, _, cdn_requests = playback

    response = http.get(f"/api/assets/{uuid4()}/video/playback", headers=HEADERS)

    assert response.status_code == 206
    assert response.content == b"0123"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-range"] == "bytes 0-3/100"
    assert response.headers["accept-ranges"] == "bytes"
    assert "content-disposition" not in response.headers
    assert [str(r.url) for r in cdn_requests] == [CDN_URL]
    assert cdn_requests[0].headers["range"] == "bytes=0-3"


@pytest.mark.parametrize("mimetype", ["image/svg+xml", "image/png"])
def test_non_video_asset_is_not_found_and_never_proxied(playback, mimetype):
    http, sdk, _, cdn_requests = playback
    sdk.assets.retrieve.return_value = _asset(mimetype)

    response = http.get(f"/api/assets/{uuid4()}/video/playback", headers=HEADERS)

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json()["message"] == "Asset not found or asset is not a video"
    assert cdn_requests == []


def test_asset_the_api_denies_is_not_found_and_never_proxied(playback):
    http, sdk, _, cdn_requests = playback
    sdk.assets.retrieve.side_effect = make_sdk_status_error(
        404, "not found", cls=NotFoundError
    )

    response = http.get(f"/api/assets/{uuid4()}/video/playback", headers=HEADERS)

    assert response.status_code == 404
    assert cdn_requests == []


def test_unknown_session_is_unauthorized_before_any_lookup(playback):
    http, sdk, store, cdn_requests = playback
    store.get_by_id.return_value = None

    response = http.get(f"/api/assets/{uuid4()}/video/playback", headers=HEADERS)

    assert response.status_code == 401
    sdk.assets.retrieve.assert_not_awaited()
    assert cdn_requests == []
