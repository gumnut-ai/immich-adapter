import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from tests.conftest import make_gumnut_asset, make_sdk_status_error
from routers.api.assets import view_asset
from routers.immich_models import AssetMediaSize
from routers.utils.thumbnail_cache import ThumbnailCacheContext
from services.thumbnail_cache import ThumbnailCache


def asset(url="https://cdn.example.com/current", thumbhash="known-hash"):
    result = make_gumnut_asset()
    result.width, result.height = 100, 100
    result.mime_type = "image/jpeg"
    result.thumbhash = thumbhash
    result.asset_urls = {
        rung: SimpleNamespace(url=url + "/" + rung, mimetype="image/webp")
        for rung in ["thumbnail", "preview"]
    }
    return result


@pytest.fixture
def cache():
    return ThumbnailCache(30, 100)


@pytest.mark.anyio
async def test_sequential_wave_uses_alias_then_changed_c_revalidates(cache):
    client = Mock()
    client.assets.retrieve = AsyncMock(return_value=asset())
    context = ThumbnailCacheContext("credential-library-session", None)
    ids = [uuid4() for _ in range(20)]
    with (
        patch("routers.api.assets.get_thumbnail_cache", return_value=cache),
        patch("routers.api.assets.stream_from_cdn", new_callable=AsyncMock) as cdn,
    ):
        for c in ["", "known-hash"]:
            for id in ids:
                await view_asset(
                    id,
                    size=AssetMediaSize.thumbnail,
                    edited=True,
                    c=c,
                    client=client,
                    cache_context=context,
                )
        assert client.assets.retrieve.await_count == 20
        assert cdn.await_count == 40
        client.assets.retrieve.return_value = asset(
            "https://cdn.example.com/edited", "changed-hash"
        )
        await view_asset(
            ids[0],
            size=AssetMediaSize.thumbnail,
            edited=True,
            c="changed-hash",
            client=client,
            cache_context=context,
        )
        assert client.assets.retrieve.await_count == 21
        assert cdn.await_args is not None
        assert cdn.await_args.args[0] == "https://cdn.example.com/edited/thumbnail"
    await cache.close()


@pytest.mark.anyio
async def test_mode_and_size_and_asset_are_isolated(cache):
    client = Mock()
    client.assets.retrieve = AsyncMock(return_value=asset())
    root = Mock(
        position=0, kind="original", width=100, height=100, mime_type="image/jpeg"
    )
    root.version_urls = {
        "thumbnail": SimpleNamespace(
            url="https://cdn.example.com/base", mimetype="image/webp"
        )
    }
    client.assets.versions.list = AsyncMock(return_value=[root])
    context = ThumbnailCacheContext("scope", None)
    id = uuid4()
    with (
        patch("routers.api.assets.get_thumbnail_cache", return_value=cache),
        patch("routers.api.assets.stream_from_cdn", new_callable=AsyncMock) as cdn,
    ):
        for _ in range(2):
            for size, edited, asset_id in [
                (AssetMediaSize.thumbnail, True, id),
                (AssetMediaSize.preview, True, id),
                (AssetMediaSize.thumbnail, False, id),
            ]:
                await view_asset(
                    asset_id,
                    size=size,
                    edited=edited,
                    client=client,
                    cache_context=context,
                )
        assert client.assets.retrieve.await_count == 2
        assert client.assets.versions.list.await_count == 1
        assert cdn.await_args is not None
        assert cdn.await_args.args[0] == "https://cdn.example.com/base"
        await view_asset(
            uuid4(),
            size=AssetMediaSize.thumbnail,
            edited=True,
            client=client,
            cache_context=context,
        )
        assert client.assets.retrieve.await_count == 3
    await cache.close()


@pytest.mark.anyio
async def test_cached_cdn_404_reauthorizes_once_and_upstream_error_propagates(cache):
    client = Mock()
    client.assets.retrieve = AsyncMock(return_value=asset())
    context = ThumbnailCacheContext("scope", None)
    id = uuid4()
    with (
        patch("routers.api.assets.get_thumbnail_cache", return_value=cache),
        patch("routers.api.assets.stream_from_cdn", new_callable=AsyncMock) as cdn,
        patch("services.thumbnail_cache.metrics.count") as count,
    ):
        await view_asset(
            id,
            size=AssetMediaSize.thumbnail,
            edited=True,
            client=client,
            cache_context=context,
        )
        client.assets.retrieve.side_effect = make_sdk_status_error(403)
        cdn.side_effect = HTTPException(404, "Asset not found")
        with pytest.raises(type(client.assets.retrieve.side_effect)):
            await view_asset(
                id,
                size=AssetMediaSize.thumbnail,
                edited=True,
                client=client,
                cache_context=context,
            )
        assert client.assets.retrieve.await_count == 2
        assert cdn.await_count == 2
        assert [
            (call.args[0], call.kwargs["attributes"]) for call in count.call_args_list
        ] == [
            (
                "thumbnail.cache.lookup",
                {
                    "cache.outcome": "miss",
                    "cache.enabled": True,
                    "cache.ttl_seconds": 30,
                },
            ),
            (
                "thumbnail.cache.lookup",
                {
                    "cache.outcome": "hit",
                    "cache.enabled": True,
                    "cache.ttl_seconds": 30,
                },
            ),
            (
                "thumbnail.cache.refresh",
                {
                    "cache.reason": "cdn_404",
                    "cache.enabled": True,
                    "cache.ttl_seconds": 30,
                },
            ),
        ]
    await cache.close()


@pytest.mark.anyio
@pytest.mark.parametrize("ttl", [0, 30], ids=["disabled", "unresolved-credential"])
async def test_missing_cache_context_counts_bypass_before_live_metadata_failure(ttl):
    client = Mock()
    client.assets.retrieve = AsyncMock(side_effect=make_sdk_status_error(403))
    settings = SimpleNamespace(thumbnail_metadata_cache_ttl_seconds=ttl)
    with (
        patch("routers.api.assets.get_settings", return_value=settings),
        patch("services.thumbnail_cache.metrics.count") as count,
    ):
        with pytest.raises(type(client.assets.retrieve.side_effect)):
            await view_asset(
                uuid4(),
                size=AssetMediaSize.thumbnail,
                edited=True,
                client=client,
                cache_context=None,
            )
        count.assert_called_once_with(
            "thumbnail.cache.lookup",
            1,
            attributes={
                "cache.outcome": "bypass",
                "cache.enabled": ttl > 0,
                "cache.ttl_seconds": ttl,
            },
        )


@pytest.mark.anyio
async def test_missing_variant_and_api_errors_are_not_cached(cache):
    client = Mock()
    metadata = asset()
    metadata.asset_urls = {}
    client.assets.retrieve = AsyncMock(return_value=metadata)
    context = ThumbnailCacheContext("scope", None)
    id = uuid4()
    with patch("routers.api.assets.get_thumbnail_cache", return_value=cache):
        for _ in range(2):
            with pytest.raises(HTTPException, match="not available"):
                await view_asset(
                    id,
                    size=AssetMediaSize.thumbnail,
                    edited=True,
                    client=client,
                    cache_context=context,
                )
        assert client.assets.retrieve.await_count == 2
    await cache.close()


@pytest.mark.anyio
async def test_credential_expiry_bypasses_previously_cached_metadata(cache):
    client = Mock()
    client.assets.retrieve = AsyncMock(return_value=asset())
    id = uuid4()
    with (
        patch("routers.api.assets.get_thumbnail_cache", return_value=cache),
        patch("routers.api.assets.stream_from_cdn", new_callable=AsyncMock),
    ):
        await view_asset(
            id,
            size=AssetMediaSize.thumbnail,
            edited=True,
            client=client,
            cache_context=ThumbnailCacheContext("scope", time.time() + 30),
        )
        await view_asset(
            id,
            size=AssetMediaSize.thumbnail,
            edited=True,
            client=client,
            cache_context=ThumbnailCacheContext("scope", time.time() - 1),
        )
        assert client.assets.retrieve.await_count == 2
    await cache.close()


@pytest.mark.anyio
@pytest.mark.parametrize("warm_failed_asset", [True, False], ids=["hit", "miss"])
async def test_cdn_404_preserves_unrelated_asset_cache(cache, warm_failed_asset):
    client = Mock()
    context = ThumbnailCacheContext("scope", None)
    unrelated_id, failed_id = uuid4(), uuid4()
    client.assets.retrieve = AsyncMock(
        return_value=asset("https://cdn.example.com/warm")
    )
    with (
        patch("routers.api.assets.get_thumbnail_cache", return_value=cache),
        patch("routers.api.assets.stream_from_cdn", new_callable=AsyncMock) as cdn,
    ):

        async def view(id, c=None):
            return await view_asset(
                id,
                size=AssetMediaSize.thumbnail,
                edited=True,
                client=client,
                cache_context=context,
                c=c,
            )

        await view(unrelated_id)
        if warm_failed_asset:
            await view(failed_id)
        reads_before_failure = client.assets.retrieve.await_count
        cdn.side_effect = HTTPException(404, "Asset not found")
        with pytest.raises(HTTPException):
            await view(failed_id, "known-hash" if warm_failed_asset else "")
        # An actual hit reauthorizes once; a fresh miss performs only its
        # initial metadata read. Neither failure flushes the unrelated asset.
        assert client.assets.retrieve.await_count == reads_before_failure + 1
        cdn.side_effect = None
        await view(unrelated_id, "known-hash")
        assert client.assets.retrieve.await_count == reads_before_failure + 1
        await view(failed_id)
        assert client.assets.retrieve.await_count == reads_before_failure + 2
    await cache.close()
