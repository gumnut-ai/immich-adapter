from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import Request
from pydantic import ValidationError

from config.settings import TestSettings
from routers.utils.thumbnail_cache import get_video_cache_context
from services.thumbnail_cache import ThumbnailVariant
from services.video_cache import is_cacheable_video_selection


def test_video_settings_are_independently_opt_in_and_bounded():
    settings = TestSettings()
    assert settings.video_metadata_cache_ttl_seconds == 0
    assert settings.thumbnail_metadata_cache_ttl_seconds == 30
    for ttl in [-1, 5.1]:
        with pytest.raises(ValidationError):
            TestSettings(video_metadata_cache_ttl_seconds=ttl)
    for size in [0, 10001]:
        with pytest.raises(ValidationError):
            TestSettings(video_metadata_cache_max_entries=size)


def test_video_context_enabled_independently_of_thumbnails():
    req = Request({"type": "http"})
    req.state.jwt_token = "apikey_example"
    req.state.session_token = None
    settings = SimpleNamespace(
        video_metadata_cache_ttl_seconds=5,
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
        settings.video_metadata_cache_ttl_seconds = 0
        assert get_video_cache_context(req, Mock()) is None


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
