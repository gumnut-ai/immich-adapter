import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import Request
from pydantic import ValidationError

from config.settings import TestSettings
from routers.utils.thumbnail_cache import _jwt_expiry, get_thumbnail_cache_context


def token(exp):
    claims = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    )
    return "header." + claims + ".signature"


def request(credential, session):
    result = Request({"type": "http"})
    result.state.jwt_token = credential
    result.state.session_token = session
    return result


def test_scope_isolates_credentials_sessions_libraries_and_backend():
    settings = SimpleNamespace(
        thumbnail_metadata_cache_ttl_seconds=30,
        gumnut_api_base_url="https://api.example.com",
    )
    keys = []
    credential_jwt = token(time.time() + 60)
    with (
        patch("routers.utils.thumbnail_cache.get_settings", return_value=settings),
        patch("routers.utils.thumbnail_cache.get_bound_library_id") as library,
    ):
        for credential, session, library_id in [
            (credential_jwt, "session-one", "library-one"),
            (credential_jwt, "session-two", "library-one"),
            ("apikey_one", None, "library-one"),
            ("apikey_two", None, "library-one"),
            ("apikey_one", None, "library-two"),
        ]:
            library.return_value = library_id
            context = get_thumbnail_cache_context(request(credential, session), Mock())
            assert context is not None
            assert credential not in context.scope
            keys.append(context.scope)
        settings.gumnut_api_base_url = "https://another.example.com"
        context = get_thumbnail_cache_context(request("apikey_one", None), Mock())
        assert context is not None
        keys.append(context.scope)
    assert len(set(keys)) == 6


@pytest.mark.parametrize(
    "credential",
    [
        "invalid",
        token(0),
        token(True),
        token("tomorrow"),
        token(float("inf")),
        "header.invalid.signature",
    ],
)
def test_invalid_or_expired_session_credential_stays_live(credential):
    with (
        patch(
            "routers.utils.thumbnail_cache.get_settings",
            return_value=SimpleNamespace(thumbnail_metadata_cache_ttl_seconds=30),
        ),
        patch(
            "routers.utils.thumbnail_cache.get_bound_library_id", return_value="library"
        ),
    ):
        assert (
            get_thumbnail_cache_context(request(credential, "session"), Mock()) is None
        )


def test_no_bound_library_and_explicit_disable_stay_live():
    settings = SimpleNamespace(thumbnail_metadata_cache_ttl_seconds=30)
    with (
        patch("routers.utils.thumbnail_cache.get_settings", return_value=settings),
        patch("routers.utils.thumbnail_cache.get_bound_library_id", return_value=None),
    ):
        assert get_thumbnail_cache_context(request("apikey_one", None), Mock()) is None
        settings.thumbnail_metadata_cache_ttl_seconds = 0
        assert get_thumbnail_cache_context(request("apikey_one", None), Mock()) is None


def test_settings_default_enabled_and_reject_unbounded_cache():
    settings = TestSettings()
    assert settings.thumbnail_metadata_cache_ttl_seconds == 30
    for ttl in [-1, 31]:
        with pytest.raises(ValidationError):
            TestSettings(thumbnail_metadata_cache_ttl_seconds=ttl)
    for capacity in [0, 10001]:
        with pytest.raises(ValidationError):
            TestSettings(thumbnail_metadata_cache_max_entries=capacity)


def test_jwt_expiry_is_only_an_expiry_gate():
    expiry = time.time() + 10
    assert _jwt_expiry(token(expiry)) == expiry
