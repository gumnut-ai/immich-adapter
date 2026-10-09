import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
import httpx
from fastapi import HTTPException, Request, Response

from routers.api.assets import play_asset_video
from routers.utils.cdn_client import CDNAssetNotFound
from routers.utils.thumbnail_cache import ThumbnailCacheContext
from services.thumbnail_cache import ThumbnailCache
from services.video_cache import is_cacheable_video_selection
from tests.conftest import make_gumnut_asset, make_sdk_status_error


def video(url=None):
    result = make_gumnut_asset()
    result.width, result.height = 100, 100
    result.mime_type = "video/mp4"
    result.thumbhash = "video-thumbhash"
    result.asset_urls = {
        "original": SimpleNamespace(
            url=url or "https://assets.gumnut.ai/version/original?verify=" + "a" * 43,
            mimetype="video/mp4",
        )
    }
    return result


def request(range_header="bytes=0-1"):
    return Request({"type": "http", "headers": [(b"range", range_header.encode())]})


@pytest.mark.anyio
async def test_range_burst_reuses_only_selection_and_expires():
    now = [0.0]
    cache = ThumbnailCache(
        5,
        10,
        clock=lambda: now[0],
        namespace="video",
        cacheable=is_cacheable_video_selection,
    )
    sdk = Mock()
    sdk.assets.retrieve = AsyncMock(return_value=video())
    context = ThumbnailCacheContext("credential-library-session", None)
    asset_id = uuid4()
    with (
        patch("routers.api.assets.get_video_cache", return_value=cache),
        patch(
            "routers.api.assets.stream_from_cdn",
            AsyncMock(side_effect=lambda *a, **k: Response(status_code=206)),
        ) as cdn,
        patch("services.thumbnail_cache.metrics.count") as count,
    ):
        for byte_range in ["bytes=0-1", "bytes=2-500", "bytes=501-"]:
            response = await play_asset_video(
                asset_id, request(byte_range), client=sdk, cache_context=context
            )
            assert response.status_code == 206
        assert sdk.assets.retrieve.await_count == 1
        assert [call.kwargs["range_header"] for call in cdn.call_args_list] == [
            "bytes=0-1",
            "bytes=2-500",
            "bytes=501-",
        ]
        assert len(cache._entries) == 1  # Video creates no thumbhash alias.
        assert [
            (call.args[0], call.kwargs["attributes"]["cache.outcome"])
            for call in count.call_args_list
        ] == [
            ("video.cache.lookup", "miss"),
            ("video.cache.lookup", "hit"),
            ("video.cache.lookup", "hit"),
        ]
        now[0] = 5
        await play_asset_video(asset_id, request(), client=sdk, cache_context=context)
        assert sdk.assets.retrieve.await_count == 2
    await cache.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "https://unknown.example/video?expires=123",
        "https://assets.gumnut.ai/video?verify=" + "a" * 43 + "&expires=123",
        "https://assets.gumnut.ai/unsigned",
    ],
)
async def test_unknown_capability_is_not_retained(url):
    cache = ThumbnailCache(
        5, 10, namespace="video", cacheable=is_cacheable_video_selection
    )
    sdk = Mock()
    sdk.assets.retrieve = AsyncMock(return_value=video(url))
    asset_id = uuid4()
    with (
        patch("routers.api.assets.get_video_cache", return_value=cache),
        patch("routers.api.assets.stream_from_cdn", AsyncMock()),
    ):
        for _ in range(2):
            await play_asset_video(
                asset_id,
                request(),
                client=sdk,
                cache_context=ThumbnailCacheContext("scope", None),
            )
        assert sdk.assets.retrieve.await_count == 2
        assert not cache._entries
    await cache.close()


@pytest.mark.anyio
async def test_asset_and_credential_scopes_do_not_share_and_expired_credential_stays_live():
    cache = ThumbnailCache(
        5, 10, namespace="video", cacheable=is_cacheable_video_selection
    )
    sdk = Mock()
    sdk.assets.retrieve = AsyncMock(return_value=video())
    first, second = uuid4(), uuid4()
    with (
        patch("routers.api.assets.get_video_cache", return_value=cache),
        patch("routers.api.assets.stream_from_cdn", AsyncMock()),
    ):
        for asset_id, context in [
            (first, ThumbnailCacheContext("one", None)),
            (first, ThumbnailCacheContext("two", None)),
            (second, ThumbnailCacheContext("one", None)),
            (first, ThumbnailCacheContext("one", time.time() - 1)),
        ]:
            await play_asset_video(
                asset_id, request(), client=sdk, cache_context=context
            )
        assert sdk.assets.retrieve.await_count == 4
    await cache.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "warm,status", [(True, 404), (False, 404), (True, 403), (True, 502)]
)
async def test_only_stored_hit_cdn_404_refreshes_and_preserves_range(warm, status):
    cache = ThumbnailCache(
        5, 10, namespace="video", cacheable=is_cacheable_video_selection
    )
    sdk = Mock()
    sdk.assets.retrieve = AsyncMock(return_value=video())
    asset_id = uuid4()
    context = ThumbnailCacheContext("scope", None)
    with (
        patch("routers.api.assets.get_video_cache", return_value=cache),
        patch("routers.api.assets.stream_from_cdn", AsyncMock()) as cdn,
    ):
        if warm:
            await play_asset_video(
                asset_id, request(), client=sdk, cache_context=context
            )
        reads_before = sdk.assets.retrieve.await_count
        error = (
            CDNAssetNotFound(status) if status in {403, 404} else HTTPException(status)
        )
        cdn.side_effect = [error, Response(status_code=206)]
        if warm and status == 404:
            response = await play_asset_video(
                asset_id, request("bytes=100-200"), client=sdk, cache_context=context
            )
            assert response.status_code == 206
            assert cdn.call_args_list[-1].kwargs["range_header"] == "bytes=100-200"
            assert sdk.assets.retrieve.await_count == reads_before + 1
        else:
            with pytest.raises(HTTPException) as exc:
                await play_asset_video(
                    asset_id, request(), client=sdk, cache_context=context
                )
            assert exc.value.status_code == (404 if status == 403 else status)
            assert sdk.assets.retrieve.await_count == reads_before + (0 if warm else 1)
    await cache.close()


@pytest.mark.anyio
async def test_real_cdn_403_on_cached_selection_never_refreshes_metadata():
    cache = ThumbnailCache(
        5, 10, namespace="video", cacheable=is_cacheable_video_selection
    )
    sdk = Mock()
    sdk.assets.retrieve = AsyncMock(return_value=video())
    asset_id = uuid4()
    context = ThumbnailCacheContext("scope", None)
    statuses = iter([206, 403])
    cdn = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(next(statuses), content=b"ok", request=req)
        )
    )
    try:
        with (
            patch("routers.api.assets.get_video_cache", return_value=cache),
            patch(
                "routers.utils.cdn_client.get_cdn_http_client",
                AsyncMock(return_value=cdn),
            ),
        ):
            first = await play_asset_video(
                asset_id, request(), client=sdk, cache_context=context
            )
            async for _ in first.body_iterator:
                pass
            with pytest.raises(CDNAssetNotFound) as exc:
                await play_asset_video(
                    asset_id, request(), client=sdk, cache_context=context
                )
            assert exc.value.status_code == 404
            assert exc.value.upstream_status == 403
            assert sdk.assets.retrieve.await_count == 1
    finally:
        await cdn.aclose()
        await cache.close()


@pytest.mark.anyio
async def test_metadata_failure_is_live_and_never_cached():
    cache = ThumbnailCache(
        5, 10, namespace="video", cacheable=is_cacheable_video_selection
    )
    sdk = Mock()
    sdk.assets.retrieve = AsyncMock(side_effect=make_sdk_status_error(401, "revoked"))
    asset_id = uuid4()
    with (
        patch("routers.api.assets.get_video_cache", return_value=cache),
        patch("routers.api.assets.stream_from_cdn", AsyncMock()) as cdn,
    ):
        for _ in range(2):
            with pytest.raises(Exception):
                await play_asset_video(
                    asset_id,
                    request(),
                    client=sdk,
                    cache_context=ThumbnailCacheContext("scope", None),
                )
        assert sdk.assets.retrieve.await_count == 2
        cdn.assert_not_awaited()
    await cache.close()
