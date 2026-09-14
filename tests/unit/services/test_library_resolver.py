"""Unit tests for library resolution and the per-credential cache."""

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import redis.exceptions

from services.library_resolver import (
    API_KEY_LIBRARY_TTL_SECONDS,
    LibraryCache,
    first_live_library_id,
    is_library_not_found,
)
from tests.conftest import make_gumnut_library


class TestFirstLiveLibraryId:
    def test_picks_oldest_even_when_listed_newest_first(self):
        """The API lists live libraries newest first; the choice is the oldest."""
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        libraries = [
            make_gumnut_library("lib_new", base + timedelta(days=2)),
            make_gumnut_library("lib_mid", base + timedelta(days=1)),
            make_gumnut_library("lib_old", base),
        ]

        assert first_live_library_id(libraries) == "lib_old"

    def test_ties_break_on_id(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        libraries = [
            make_gumnut_library("lib_b", base),
            make_gumnut_library("lib_a", base),
        ]

        assert first_live_library_id(libraries) == "lib_a"

    def test_none_when_no_live_library(self):
        assert first_live_library_id([]) is None


class TestIsLibraryNotFound:
    @pytest.mark.parametrize(
        "detail",
        [
            "Library lib_gone not found or not accessible by user intuser_1",
            "Library 'lib_gone' not found or not accessible by user intuser_1",
            "Library lib_gone not found or not accessible",
        ],
    )
    def test_matches_every_shape_the_api_emits(self, detail):
        assert is_library_not_found(detail, "lib_gone") is True

    def test_ignores_other_library(self):
        detail = "Library lib_gone not found or not accessible by user intuser_1"

        assert is_library_not_found(detail, "lib_other") is False

    def test_ignores_other_entity_404(self):
        assert is_library_not_found("Asset asset_1 not found", "lib_gone") is False


API_KEY = "apikey_secret"
API_KEY_CACHE_KEY = f"library:apikey:{hashlib.sha256(API_KEY.encode()).hexdigest()}"
SESSION_TOKEN = "550e8400-e29b-41d4-a716-446655440000"


class TestLibraryCache:
    @pytest.fixture
    def mock_redis(self):
        return AsyncMock()

    @pytest.fixture
    def mock_session_store(self):
        return AsyncMock()

    @pytest.fixture
    def cache(self, mock_redis, mock_session_store):
        return LibraryCache(mock_redis, mock_session_store)

    @pytest.mark.anyio
    async def test_api_key_hit_reads_hashed_key(self, cache, mock_redis):
        mock_redis.get.return_value = "lib_cached"

        assert await cache.get_for_api_key(API_KEY) == "lib_cached"
        mock_redis.get.assert_awaited_once_with(API_KEY_CACHE_KEY)

    @pytest.mark.anyio
    async def test_api_key_miss(self, cache, mock_redis):
        mock_redis.get.return_value = None

        assert await cache.get_for_api_key(API_KEY) is None

    @pytest.mark.anyio
    async def test_api_key_redis_failure_is_a_miss(self, cache, mock_redis):
        mock_redis.get.side_effect = redis.exceptions.ConnectionError("down")

        assert await cache.get_for_api_key(API_KEY) is None

    @pytest.mark.anyio
    async def test_remember_for_session_writes_session_field(
        self, cache, mock_session_store
    ):
        await cache.remember_for_session(SESSION_TOKEN, "lib_1")

        mock_session_store.update_library_id.assert_awaited_once_with(
            SESSION_TOKEN, "lib_1"
        )

    @pytest.mark.anyio
    async def test_remember_for_api_key_writes_hashed_key_with_ttl(
        self, cache, mock_redis
    ):
        await cache.remember_for_api_key(API_KEY, "lib_1")

        mock_redis.set.assert_awaited_once_with(
            API_KEY_CACHE_KEY, "lib_1", ex=API_KEY_LIBRARY_TTL_SECONDS
        )

    @pytest.mark.anyio
    async def test_remember_redis_failure_is_swallowed(self, cache, mock_redis):
        mock_redis.set.side_effect = redis.exceptions.ConnectionError("down")

        await cache.remember_for_api_key(API_KEY, "lib_1")

    @pytest.mark.anyio
    async def test_forget_session_drops_library_and_flags_reset(
        self, cache, mock_session_store
    ):
        await cache.forget_session(SESSION_TOKEN)

        mock_session_store.forget_library.assert_awaited_once_with(SESSION_TOKEN)

    @pytest.mark.anyio
    async def test_forget_api_key_deletes_hashed_key(self, cache, mock_redis):
        await cache.forget_api_key(API_KEY)

        mock_redis.delete.assert_awaited_once_with(API_KEY_CACHE_KEY)

    @pytest.mark.anyio
    async def test_forget_redis_failure_is_swallowed(self, cache, mock_redis):
        mock_redis.delete.side_effect = redis.exceptions.ConnectionError("down")

        await cache.forget_api_key(API_KEY)
