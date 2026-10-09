import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from routers.api.assets import router
from routers.middleware.auth_middleware import AuthMiddleware
from routers.utils.gumnut_client import (
    LibraryScope,
    bind_library_scope,
    get_authenticated_gumnut_client,
)
from services.thumbnail_cache import ThumbnailCache
from services.video_cache import is_cacheable_video_selection


def test_video_session_rechecked_range_forwarded_and_failed_mutation_invalidates():
    url = "https://assets.gumnut.ai/version/old?verify=" + "a" * 43
    metadata = Mock(width=100, height=100, mime_type="video/mp4", thumbhash="hash")
    metadata.asset_urls = {"original": SimpleNamespace(url=url, mimetype="video/mp4")}
    sdk = Mock()
    sdk.assets.retrieve = AsyncMock(return_value=metadata)
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
    settings = SimpleNamespace(
        gumnut_api_base_url="https://api.example.com",
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    async def authenticated():
        bind_library_scope(LibraryScope("library", AsyncMock()))
        return sdk

    async def mutate():
        metadata.asset_urls["original"].url = (
            "https://assets.gumnut.ai/version/new?verify=" + "a" * 43
        )
        return Response(status_code=500)

    app.add_api_route("/api/assets/example/edits", mutate, methods=["PUT"])
    app.include_router(router)
    app.dependency_overrides[get_authenticated_gumnut_client] = authenticated

    async def stream(url, mimetype, *, range_header):
        return Response(
            url,
            status_code=206,
            media_type=mimetype,
            headers={"content-range": range_header, "accept-ranges": "bytes"},
        )

    with (
        patch(
            "routers.middleware.auth_middleware.get_session_store",
            AsyncMock(return_value=store),
        ),
        patch("routers.middleware.auth_middleware.get_video_cache", return_value=cache),
        patch("routers.api.assets.get_video_cache", return_value=cache),
        patch("routers.utils.thumbnail_cache.get_settings", return_value=settings),
        patch("routers.api.assets.stream_from_cdn", AsyncMock(side_effect=stream)),
    ):
        with TestClient(app) as http:
            path = f"/api/assets/{uuid4()}/video/playback"
            headers = {"authorization": "Bearer session-token", "range": "bytes=0-1"}
            first = http.get(path, headers=headers)
            assert first.status_code == 206
            assert first.headers["content-range"] == "bytes=0-1"
            assert first.headers["accept-ranges"] == "bytes"
            headers["range"] = "bytes=2-999"
            assert (
                http.get(path, headers=headers).headers["content-range"]
                == "bytes=2-999"
            )
            assert sdk.assets.retrieve.await_count == 1
            assert store.get_by_id.await_count == 2
            assert (
                http.put("/api/assets/example/edits", headers=headers).status_code
                == 500
            )
            assert "/version/new" in http.get(path, headers=headers).text
            assert sdk.assets.retrieve.await_count == 2
            store.get_by_id.return_value = None
            assert http.get(path, headers=headers).status_code == 401
            assert sdk.assets.retrieve.await_count == 2


def test_unresolved_video_scope_performs_live_read_per_range():
    app = FastAPI()
    app.include_router(router)
    sdk = Mock()
    sdk.assets.retrieve = AsyncMock(
        return_value=Mock(
            width=100,
            height=100,
            mime_type="video/mp4",
            thumbhash=None,
            asset_urls={
                "original": SimpleNamespace(
                    url="https://assets.gumnut.ai/old?verify=" + "a" * 43,
                    mimetype="video/mp4",
                )
            },
        )
    )
    app.dependency_overrides[get_authenticated_gumnut_client] = lambda: sdk
    with (
        patch(
            "routers.utils.thumbnail_cache.get_settings",
            return_value=SimpleNamespace(gumnut_api_base_url="https://api.example.com"),
        ),
        patch(
            "routers.utils.thumbnail_cache.get_bound_library_id",
            return_value=None,
        ),
        patch(
            "routers.api.assets.stream_from_cdn",
            AsyncMock(return_value=Response(status_code=206)),
        ),
    ):
        with TestClient(app) as http:
            path = f"/api/assets/{uuid4()}/video/playback"
            for byte_range in ["bytes=0-1", "bytes=2-"]:
                assert http.get(path, headers={"range": byte_range}).status_code == 206
            assert sdk.assets.retrieve.await_count == 2
