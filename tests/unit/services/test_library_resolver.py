"""Unit tests for library resolution and the per-credential cache."""

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import redis.exceptions

from services.library_resolver import (
    API_KEY_LIBRARY_TTL_SECONDS,
    LibraryCache,
    LibraryChoice,
    choose_library,
    first_owned_library_id,
    is_library_not_found,
)
from tests.conftest import make_gumnut_library


class TestFirstOwnedLibraryId:
    def test_picks_oldest_even_when_listed_newest_first(self):
        """The API lists live libraries newest first; the choice is the oldest."""
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        libraries = [
            make_gumnut_library("lib_new", base + timedelta(days=2)),
            make_gumnut_library("lib_mid", base + timedelta(days=1)),
            make_gumnut_library("lib_old", base),
        ]

        assert first_owned_library_id(libraries) == "lib_old"

    def test_ties_break_on_id(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        libraries = [
            make_gumnut_library("lib_b", base),
            make_gumnut_library("lib_a", base),
        ]

        assert first_owned_library_id(libraries) == "lib_a"

    def test_none_when_no_live_library(self):
        assert first_owned_library_id([]) is None

    @pytest.mark.parametrize("role", ["viewer", "collaborator"])
    def test_older_joined_library_does_not_win(self, role):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        libraries = [
            make_gumnut_library("lib_owned", base + timedelta(days=1)),
            make_gumnut_library("lib_joined", base, role=role),
        ]

        assert first_owned_library_id(libraries) == "lib_owned"

    def test_oldest_owned_wins_among_several(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        libraries = [
            make_gumnut_library("lib_owned_newer", base + timedelta(days=3)),
            make_gumnut_library("lib_owned_older", base + timedelta(days=2)),
            make_gumnut_library("lib_joined", base, role="viewer"),
        ]

        assert first_owned_library_id(libraries) == "lib_owned_older"

    def test_none_when_only_joined_libraries(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        libraries = [make_gumnut_library("lib_joined", base, role="collaborator")]

        assert first_owned_library_id(libraries) is None


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWNED_OLD = make_gumnut_library("lib_owned_old", BASE)
OWNED_NEW = make_gumnut_library("lib_owned_new", BASE + timedelta(days=1))
SHARED = make_gumnut_library(
    "lib_shared", BASE - timedelta(days=1), role="collaborator"
)
VIEWED = make_gumnut_library("lib_viewed", BASE - timedelta(days=1), role="viewer")


class TestChooseLibrary:
    def test_no_choice_is_the_oldest_owned(self):
        assert choose_library([OWNED_NEW, OWNED_OLD], None) == LibraryChoice(
            "lib_owned_old", from_choice=False
        )

    @pytest.mark.parametrize("library", [OWNED_NEW, SHARED])
    def test_usable_choice_wins(self, library):
        libraries = [OWNED_OLD, OWNED_NEW, SHARED]

        assert choose_library(libraries, library.id) == LibraryChoice(
            library.id, from_choice=True
        )

    def test_choice_lifts_the_owner_filter_for_a_shared_only_user(self):
        assert choose_library([SHARED], "lib_shared") == LibraryChoice(
            "lib_shared", from_choice=True
        )

    @pytest.mark.parametrize(
        "preferred_id",
        [
            pytest.param("lib_viewed", id="viewer-role"),
            pytest.param("lib_trashed", id="not-listed"),
        ],
    )
    def test_unusable_choice_falls_back(self, preferred_id):
        libraries = [OWNED_OLD, OWNED_NEW, VIEWED]

        assert choose_library(libraries, preferred_id) == LibraryChoice(
            "lib_owned_old", from_choice=False
        )

    def test_unusable_choice_and_nothing_owned_is_none(self):
        assert choose_library([VIEWED, SHARED], "lib_viewed") is None

    def test_fallback_stays_on_a_still_owned_library(self):
        """Restoring an older library does not move a session that fell back."""
        libraries = [OWNED_OLD, OWNED_NEW]

        assert choose_library(
            libraries, None, stay_on="lib_owned_new"
        ) == LibraryChoice("lib_owned_new", from_choice=False)

    def test_usable_choice_overrides_staying_put(self):
        libraries = [OWNED_OLD, OWNED_NEW]

        assert choose_library(
            libraries, "lib_owned_old", stay_on="lib_owned_new"
        ) == LibraryChoice("lib_owned_old", from_choice=True)

    @pytest.mark.parametrize(
        "stay_on",
        [
            pytest.param("lib_shared", id="not-owned"),
            pytest.param("lib_trashed", id="not-listed"),
        ],
    )
    def test_stay_put_needs_an_owned_live_library(self, stay_on):
        libraries = [OWNED_OLD, OWNED_NEW, SHARED]

        assert choose_library(libraries, None, stay_on=stay_on) == LibraryChoice(
            "lib_owned_old", from_choice=False
        )


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
        await cache.remember_for_session(
            SESSION_TOKEN, LibraryChoice("lib_1", from_choice=True)
        )

        mock_session_store.update_library_id.assert_awaited_once_with(
            SESSION_TOKEN, "lib_1", from_choice=True
        )

    @pytest.mark.anyio
    async def test_switch_session_moves_library_through_the_store(
        self, cache, mock_session_store
    ):
        await cache.switch_session(
            SESSION_TOKEN, "lib_old", LibraryChoice("lib_new", from_choice=False)
        )

        mock_session_store.switch_library.assert_awaited_once_with(
            SESSION_TOKEN, "lib_old", "lib_new", from_choice=False
        )

    @pytest.mark.anyio
    async def test_switch_session_redis_failure_is_swallowed(
        self, cache, mock_session_store
    ):
        mock_session_store.switch_library.side_effect = (
            redis.exceptions.ConnectionError("down")
        )

        await cache.switch_session(
            SESSION_TOKEN, "lib_old", LibraryChoice("lib_new", from_choice=False)
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
