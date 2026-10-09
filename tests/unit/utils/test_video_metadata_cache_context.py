from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import Request
from pydantic import ValidationError

from config.settings import TestSettings
from routers.utils.thumbnail_cache import get_video_cache_context
from services.thumbnail_cache import ThumbnailVariant
from services.video_cache import is_cacheable_video_selection
from services import video_cache


def test_video_capacity_is_bounded_and_ttl_is_not_configurable():
    settings = TestSettings()
    assert "video_metadata_cache_ttl_seconds" not in TestSettings.model_fields
    assert settings.thumbnail_metadata_cache_ttl_seconds == 30
    for size in [0, 10001]:
        with pytest.raises(ValidationError):
            TestSettings(video_metadata_cache_max_entries=size)


@pytest.mark.anyio
async def test_video_factory_enables_reuse_without_a_ttl_setting(monkeypatch):
    monkeypatch.setattr(video_cache, "_cache", None)
    monkeypatch.setattr(video_cache, "get_settings", lambda: TestSettings())
    cache = video_cache.get_video_cache()
    assert cache.ttl_seconds == 5
    variant = ThumbnailVariant(
        "https://assets.gumnut.ai/version/video?verify=" + "a" * 43,
        "video/mp4",
    )
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        return variant

    try:
        assert (await cache.get("scope", None, load)).outcome == "miss"
        assert (await cache.get("scope", None, load)).outcome == "hit"
        assert calls == 1
    finally:
        await video_cache.close_video_cache()


def test_video_context_enabled_independently_of_thumbnails():
    req = Request({"type": "http"})
    req.state.jwt_token = "apikey_example"
    req.state.session_token = None
    settings = SimpleNamespace(
        thumbnail_metadata_cache_ttl_seconds=0,
        gumnut_api_base_url="https://api.example.com",
    )
    with (
        patch("routers.utils.thumbnail_cache.get_settings", return_value=settings),
        patch(
            "routers.utils.thumbnail_cache.get_bound_library_id", return_value="library"
        ),
    ):
        context = get_video_cache_context(req, Mock())
        assert context is not None
        assert "apikey_example" not in context.scope


@pytest.mark.parametrize("suffix", ["", "&dl=movie.mp4"])
def test_known_nonexpiring_capability_is_eligible(suffix):
    assert is_cacheable_video_selection(
        ThumbnailVariant(
            "https://assets.gumnut.ai/version/video?verify=" + "a" * 43 + suffix,
            "video/mp4",
        )
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://assets.gumnut.ai/version?verify=invalid",
        "https://assets.gumnut.ai/version?verify=" + "a" * 43 + "&verify=" + "a" * 43,
        "http://assets.gumnut.ai/version?verify=" + "a" * 43,
        "https://assets.gumnut.ai:bad/version?verify=" + "a" * 43,
    ],
)
def test_malformed_or_unknown_capabilities_are_not_eligible(url):
    assert not is_cacheable_video_selection(ThumbnailVariant(url, "video/mp4"))
