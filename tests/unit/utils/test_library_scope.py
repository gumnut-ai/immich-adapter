"""Unit tests for binding the resolved library into the Gumnut client."""

import asyncio
import inspect
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import fakeredis
import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from gumnut import AsyncGumnut, NotFoundError, PermissionDeniedError
from redis.exceptions import MaxConnectionsError

from config.exceptions import configure_exception_handlers
from routers.api.albums import create_album
from routers.api.assets import upload_asset
from routers.api.faces import create_face
from routers.api.people import create_person
from routers.api.search import search_assets
from routers.api.stacks import create_stack
from routers.immich_models import MetadataSearchDto
from routers.utils.gumnut_client import (
    LibraryScope,
    _library_choices_in_flight,
    _resolve_library_id,
    _response_hook,
    bind_library_scope,
    forget_bound_library_if_gone,
    get_authenticated_gumnut_client,
    get_bound_library_id,
    get_current_library_id,
    get_gumnut_client,
    get_refreshed_token,
    init_request_scope,
)
from services.library_resolver import (
    LIBRARY_RECHECK_SECONDS,
    LibraryCache,
    get_library_cache,
)
from services.session_store import SessionStore
from tests.conftest import make_gumnut_library, make_sdk_status_error

JWT = "test.jwt.token"
API_KEY = "apikey_abc123"
SESSION_TOKEN = "550e8400-e29b-41d4-a716-446655440000"
LIBRARY_GONE = "Library lib_bound not found or not accessible by user intuser_1"


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["lib_chosen", None, "missing"])
@pytest.mark.parametrize("role", ["owner", "collaborator"])
async def test_selected_key_binding_reaches_downstream_routes(target, role):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        if request.url.path == "/api/libraries":
            return httpx.Response(403, json={"detail": "limited to selected libraries"})
        if request.url.path == "/api/users/me":
            user = {"id": "intuser_test", "immich_library_id": None}
            if target != "missing":
                user["immich_library_id"] = target
            return httpx.Response(200, json=user)
        if request.url.path == f"/api/libraries/{target}":
            return httpx.Response(
                200,
                json={
                    "id": target,
                    "role": role,
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
        assert request.url.params.get("library_id") == target
        return httpx.Response(200, json={"data": [], "has_more": False})

    cache = AsyncMock()
    cache.get_for_api_key.return_value = None
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with (
            patch(
                "routers.utils.gumnut_client.get_shared_http_client", return_value=http
            ),
            patch("routers.utils.gumnut_client.get_settings") as settings,
        ):
            settings.return_value.gumnut_api_base_url = "http://gumnut.test"
            request = _request(jwt_token=API_KEY, session_token=None)
            if target in {None, "missing"}:
                with pytest.raises(HTTPException) as exc_info:
                    await get_authenticated_gumnut_client(request, cache)
                assert exc_info.value.status_code == 403
                assert "instead of Default" in exc_info.value.detail
                assert [r.url.path for r in seen] == ["/api/libraries", "/api/users/me"]
                cache.remember_for_api_key.assert_not_awaited()
                return
            client = await get_authenticated_gumnut_client(request, cache)
            result = await search_assets(MetadataSearchDto(), client, Mock())
            assert result.assets.count == 0
            await client.albums.list()
            await client.people.list()
            await client.faces.list()
            await client.get("/api/stacks", cast_to=dict[str, object])
            await client.events.get()
            assert {r.url.path for r in seen[3:]} == {
                "/api/assets",
                "/api/albums",
                "/api/people",
                "/api/faces",
                "/api/stacks",
                "/api/events",
            }
            assert await get_current_library_id(client) == target
            cache.remember_for_api_key.assert_awaited_once_with(API_KEY, target)


def _request(
    *,
    jwt_token: str | None = JWT,
    session_token: str | None = SESSION_TOKEN,
    session_library_id: str | None = None,
    stale: bool = False,
    from_choice: bool = False,
) -> Mock:
    request = Mock()
    request.state = type("State", (), {})()
    request.state.jwt_token = jwt_token
    request.state.session_token = session_token
    request.state.session_library_id = session_library_id
    request.state.session_library_checked_at = time.time() - (
        LIBRARY_RECHECK_SECONDS + 1 if stale else 0
    )
    request.state.session_library_from_choice = from_choice
    return request


@pytest.fixture(autouse=True)
def fresh_scope():
    init_request_scope()
    _library_choices_in_flight.clear()


def _mock_transport_client(
    library_id: str | None, handler: httpx.MockTransport
) -> AsyncGumnut:
    return AsyncGumnut(
        api_key=JWT,
        base_url="http://gumnut.test",
        default_query={"library_id": library_id} if library_id else None,
        http_client=httpx.AsyncClient(
            transport=handler, event_hooks={"response": [_response_hook]}
        ),
        max_retries=0,
    )


class TestDefaultQueryBinding:
    """The SDK contract the design depends on, pinned against the wire."""

    @staticmethod
    def _client(library_id: str | None, seen: list[str]) -> AsyncGumnut:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": [], "has_more": False})

        return _mock_transport_client(library_id, httpx.MockTransport(handler))

    @pytest.mark.anyio
    async def test_bound_library_reaches_a_call_that_omits_it(self):
        seen: list[str] = []
        client = self._client("lib_bound", seen)

        await client.assets.list()

        assert seen == ["http://gumnut.test/api/assets?library_id=lib_bound"]

    @pytest.mark.anyio
    async def test_explicit_call_value_wins(self):
        seen: list[str] = []
        client = self._client("lib_bound", seen)

        await client.assets.list(library_id="lib_explicit")

        assert seen == ["http://gumnut.test/api/assets?library_id=lib_explicit"]

    @pytest.mark.anyio
    async def test_unbound_client_sends_nothing(self):
        seen: list[str] = []
        client = self._client(None, seen)

        await client.assets.list()

        assert seen == ["http://gumnut.test/api/assets"]

    @pytest.mark.anyio
    async def test_get_gumnut_client_binds_library(self):
        with patch("routers.utils.gumnut_client.get_settings") as mock_settings:
            mock_settings.return_value.gumnut_api_base_url = "http://gumnut.test"
            client = await get_gumnut_client(JWT, "lib_bound")

        assert client.default_query == {"library_id": "lib_bound"}


BASE = datetime(2026, 1, 1, tzinfo=UTC)
OWNED_OLD = make_gumnut_library("lib_old", BASE)
OWNED_NEW = make_gumnut_library("lib_new", BASE + timedelta(days=1))
SHARED = make_gumnut_library("lib_shared", BASE, role="collaborator")


class TestResolveLibraryId:
    @pytest.fixture
    def cache(self):
        cache = AsyncMock()
        cache.get_for_api_key.return_value = None
        cache.set_session_library.return_value = True
        return cache

    @pytest.fixture
    def unscoped_client(self):
        client = Mock()
        client.libraries.list = AsyncMock(return_value=[])
        client.libraries.retrieve = AsyncMock()
        client.users.me = AsyncMock(return_value=Mock(immich_library_id=None))
        return client

    @pytest.fixture
    def get_client(self, unscoped_client):
        with patch(
            "routers.utils.gumnut_client.get_gumnut_client",
            AsyncMock(return_value=unscoped_client),
        ) as mock:
            yield mock

    @pytest.mark.anyio
    async def test_session_cached_library_skips_lookup(
        self, cache, unscoped_client, get_client
    ):
        request = _request(session_library_id="lib_cached")

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id == "lib_cached"
        assert get_bound_library_id() == "lib_cached"
        unscoped_client.libraries.list.assert_not_awaited()
        cache.set_session_library.assert_not_awaited()

    @pytest.mark.anyio
    async def test_session_miss_lists_oldest_live_library_and_caches(
        self, cache, unscoped_client, get_client
    ):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        unscoped_client.libraries.list.return_value = [
            make_gumnut_library("lib_new", base.replace(day=2)),
            make_gumnut_library("lib_old", base),
        ]
        request = _request(session_library_id=None)

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id == "lib_old"
        assert get_bound_library_id() == "lib_old"
        # The lookup runs on an unscoped client, without a library bound.
        get_client.assert_awaited_once_with(JWT)
        cache.set_session_library.assert_awaited_once_with(
            SESSION_TOKEN, "", "lib_old", from_choice=False
        )
        # A session's first resolution is not a change of library.

    @pytest.mark.anyio
    async def test_session_miss_skips_an_older_joined_library(
        self, cache, unscoped_client, get_client
    ):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        unscoped_client.libraries.list.return_value = [
            make_gumnut_library("lib_owned", base.replace(day=2)),
            make_gumnut_library("lib_joined", base, role="collaborator"),
        ]
        request = _request(session_library_id=None)

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id == "lib_owned"
        cache.set_session_library.assert_awaited_once_with(
            SESSION_TOKEN, "", "lib_owned", from_choice=False
        )

    @pytest.mark.anyio
    async def test_api_key_reads_its_own_cache(
        self, cache, unscoped_client, get_client
    ):
        cache.get_for_api_key.return_value = "lib_cached"
        request = _request(jwt_token=API_KEY, session_token=None)

        library_id = await _resolve_library_id(request, API_KEY, cache)

        assert library_id == "lib_cached"
        cache.get_for_api_key.assert_awaited_once_with(API_KEY)
        unscoped_client.libraries.list.assert_not_awaited()

    @pytest.mark.anyio
    async def test_api_key_miss_caches_under_the_key(
        self, cache, unscoped_client, get_client
    ):
        unscoped_client.libraries.list.return_value = [
            make_gumnut_library("lib_only", datetime(2026, 1, 1, tzinfo=UTC))
        ]
        request = _request(jwt_token=API_KEY, session_token=None)

        library_id = await _resolve_library_id(request, API_KEY, cache)

        assert library_id == "lib_only"
        cache.remember_for_api_key.assert_awaited_once_with(API_KEY, "lib_only")

    @pytest.mark.anyio
    async def test_no_live_library_leaves_request_unscoped(
        self, cache, unscoped_client, get_client
    ):
        request = _request(session_library_id=None)

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id is None
        assert get_bound_library_id() is None
        cache.set_session_library.assert_not_awaited()

    @pytest.mark.anyio
    async def test_only_joined_libraries_is_refused_not_left_unscoped(
        self, cache, unscoped_client, get_client
    ):
        """Unscoped, the API would default to the lone shared library."""
        unscoped_client.libraries.list.return_value = [
            make_gumnut_library(
                "lib_joined",
                datetime(2026, 1, 1, tzinfo=UTC),
                role="collaborator",
            )
        ]
        request = _request(session_library_id=None)

        with pytest.raises(HTTPException) as exc_info:
            await _resolve_library_id(request, JWT, cache)

        assert exc_info.value.status_code == 403
        assert "web settings" in exc_info.value.detail
        assert get_bound_library_id() is None
        cache.set_session_library.assert_not_awaited()

    @pytest.mark.anyio
    async def test_scoped_key_uses_explicit_immich_library(
        self, cache, unscoped_client, get_client
    ):
        unscoped_client.libraries.list.side_effect = make_sdk_status_error(
            403, "limited to selected libraries", cls=PermissionDeniedError
        )
        unscoped_client.users.me.return_value = Mock(immich_library_id="lib_new")
        unscoped_client.libraries.retrieve.return_value = OWNED_NEW
        request = _request(jwt_token=API_KEY, session_token=None)

        library_id = await _resolve_library_id(request, API_KEY, cache)

        assert library_id == "lib_new"
        assert get_bound_library_id() == "lib_new"
        cache.remember_for_api_key.assert_awaited_once_with(API_KEY, "lib_new")
        unscoped_client.users.me.assert_awaited_once()
        unscoped_client.libraries.retrieve.assert_awaited_once_with("lib_new")

    @pytest.mark.anyio
    @pytest.mark.parametrize("target", [None, "", 123])
    async def test_scoped_key_without_usable_target_is_refused(
        self, cache, unscoped_client, get_client, target
    ):
        unscoped_client.libraries.list.side_effect = make_sdk_status_error(
            403, "limited to selected libraries", cls=PermissionDeniedError
        )
        unscoped_client.users.me.return_value = Mock(immich_library_id=target)
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_library_id(
                _request(jwt_token=API_KEY, session_token=None), API_KEY, cache
            )
        assert exc_info.value.status_code == (403 if target is None else 502)
        assert get_bound_library_id() is None
        cache.remember_for_api_key.assert_not_awaited()
        unscoped_client.libraries.retrieve.assert_not_awaited()

    @pytest.mark.anyio
    @pytest.mark.parametrize("library_status", [403, 404])
    async def test_scoped_key_cannot_access_explicit_selection(
        self, cache, unscoped_client, get_client, library_status
    ):
        unscoped_client.libraries.list.side_effect = make_sdk_status_error(
            403, "restricted", cls=PermissionDeniedError
        )
        unscoped_client.users.me.return_value = Mock(immich_library_id="lib_new")
        unscoped_client.libraries.retrieve.side_effect = make_sdk_status_error(
            library_status,
            "inaccessible library",
            cls=PermissionDeniedError if library_status == 403 else NotFoundError,
        )
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_library_id(
                _request(jwt_token=API_KEY, session_token=None), API_KEY, cache
            )
        assert exc_info.value.status_code == 403
        assert "key must cover" in exc_info.value.detail
        assert get_bound_library_id() is None
        cache.remember_for_api_key.assert_not_awaited()

    @pytest.mark.anyio
    async def test_scoped_key_viewer_selection_is_refused(
        self, cache, unscoped_client, get_client
    ):
        unscoped_client.libraries.list.side_effect = make_sdk_status_error(
            403, "restricted", cls=PermissionDeniedError
        )
        unscoped_client.users.me.return_value = Mock(immich_library_id="lib_viewer")
        unscoped_client.libraries.retrieve.return_value = make_gumnut_library(
            "lib_viewer", datetime.now(UTC), role="viewer"
        )
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_library_id(
                _request(jwt_token=API_KEY, session_token=None), API_KEY, cache
            )
        assert exc_info.value.status_code == 403
        assert "owner or collaborator" in exc_info.value.detail
        assert get_bound_library_id() is None
        cache.remember_for_api_key.assert_not_awaited()

    @pytest.mark.anyio
    async def test_api_key_miss_follows_the_stored_choice(
        self, cache, unscoped_client, get_client
    ):
        unscoped_client.libraries.list.return_value = [OWNED_OLD, OWNED_NEW]
        unscoped_client.users.me.return_value = Mock(immich_library_id="lib_new")
        request = _request(jwt_token=API_KEY, session_token=None)

        library_id = await _resolve_library_id(request, API_KEY, cache)

        assert library_id == "lib_new"
        cache.remember_for_api_key.assert_awaited_once_with(API_KEY, "lib_new")

    @pytest.mark.anyio
    async def test_shared_only_user_with_a_choice_is_scoped_to_it(
        self, cache, unscoped_client, get_client
    ):
        unscoped_client.libraries.list.return_value = [SHARED]
        unscoped_client.users.me.return_value = Mock(immich_library_id="lib_shared")
        request = _request(session_library_id=None)

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id == "lib_shared"
        cache.set_session_library.assert_awaited_once_with(
            SESSION_TOKEN, "", "lib_shared", from_choice=True
        )

    @pytest.mark.anyio
    async def test_choice_is_skipped_when_the_credential_may_not_read_it(
        self, cache, unscoped_client, get_client
    ):
        unscoped_client.libraries.list.return_value = [OWNED_OLD, OWNED_NEW]
        unscoped_client.users.me.side_effect = make_sdk_status_error(
            403, "not permitted", cls=PermissionDeniedError
        )
        request = _request(jwt_token=API_KEY, session_token=None)

        library_id = await _resolve_library_id(request, API_KEY, cache)

        assert library_id == "lib_old"

    @pytest.mark.anyio
    async def test_stale_session_with_unchanged_library_restamps_without_reset(
        self, cache, unscoped_client, get_client
    ):
        unscoped_client.libraries.list.return_value = [OWNED_OLD, OWNED_NEW]
        request = _request(session_library_id="lib_old", stale=True)

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id == "lib_old"
        cache.set_session_library.assert_awaited_once_with(
            SESSION_TOKEN, "lib_old", "lib_old", from_choice=False
        )

    @pytest.mark.anyio
    async def test_stale_session_follows_a_new_choice_with_a_reset(
        self, cache, unscoped_client, get_client
    ):
        unscoped_client.libraries.list.return_value = [OWNED_OLD, OWNED_NEW]
        unscoped_client.users.me.return_value = Mock(immich_library_id="lib_new")
        request = _request(session_library_id="lib_old", stale=True)

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id == "lib_new"
        assert get_bound_library_id() == "lib_new"
        cache.set_session_library.assert_awaited_once_with(
            SESSION_TOKEN, "lib_old", "lib_new", from_choice=True
        )

    @pytest.mark.anyio
    async def test_unrecorded_first_resolution_is_refused_for_retry(
        self, cache, unscoped_client, get_client
    ):
        """A library the session does not record must not be served: a sync
        stream would ack cursors the session cannot tie to a library."""
        cache.set_session_library.return_value = False
        unscoped_client.libraries.list.return_value = [OWNED_OLD]
        request = _request(session_library_id=None)

        with pytest.raises(HTTPException) as exc_info:
            await _resolve_library_id(request, JWT, cache)

        assert exc_info.value.status_code == 503
        assert get_bound_library_id() is None

    @pytest.mark.anyio
    async def test_unrecorded_revalidation_stays_on_the_observed_library(
        self, cache, unscoped_client, get_client
    ):
        cache.set_session_library.return_value = False
        unscoped_client.libraries.list.return_value = [OWNED_OLD]
        request = _request(session_library_id="lib_old", stale=True)

        assert await _resolve_library_id(request, JWT, cache) == "lib_old"

    @pytest.mark.anyio
    async def test_uncommitted_switch_stays_on_the_observed_library(
        self, cache, unscoped_client, get_client
    ):
        """The session still records the old library and its sync epoch, so the
        new library must not be served under them."""
        cache.set_session_library.return_value = False
        unscoped_client.libraries.list.return_value = [OWNED_OLD, OWNED_NEW]
        unscoped_client.users.me.return_value = Mock(immich_library_id="lib_new")
        request = _request(session_library_id="lib_old", stale=True)

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id == "lib_old"
        assert get_bound_library_id() == "lib_old"

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "listing,preferred_id",
        [
            pytest.param([OWNED_OLD, OWNED_NEW], None, id="choice-cleared"),
            pytest.param([OWNED_OLD], "lib_new", id="choice-trashed"),
        ],
    )
    async def test_stale_session_on_an_unusable_choice_falls_back_with_a_reset(
        self, cache, unscoped_client, get_client, listing, preferred_id
    ):
        unscoped_client.libraries.list.return_value = listing
        unscoped_client.users.me.return_value = Mock(immich_library_id=preferred_id)
        request = _request(session_library_id="lib_new", stale=True, from_choice=True)

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id == "lib_old"
        cache.set_session_library.assert_awaited_once_with(
            SESSION_TOKEN, "lib_new", "lib_old", from_choice=False
        )

    @pytest.mark.anyio
    async def test_stale_fallback_session_stays_put_when_an_older_library_returns(
        self, cache, unscoped_client, get_client
    ):
        """Without a stored choice, restoring the oldest library does not move a
        session that fell back to another one."""
        unscoped_client.libraries.list.return_value = [OWNED_OLD, OWNED_NEW]
        request = _request(session_library_id="lib_new", stale=True)

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id == "lib_new"
        cache.set_session_library.assert_awaited_once_with(
            SESSION_TOKEN, "lib_new", "lib_new", from_choice=False
        )

    @pytest.mark.anyio
    async def test_stale_session_refused_forgets_its_library(
        self, cache, unscoped_client, get_client
    ):
        unscoped_client.libraries.list.return_value = [SHARED]
        request = _request(session_library_id="lib_old", stale=True)

        with pytest.raises(HTTPException) as exc_info:
            await _resolve_library_id(request, JWT, cache)

        assert exc_info.value.status_code == 403
        cache.set_session_library.assert_awaited_once_with(SESSION_TOKEN, "lib_old", "")

    @pytest.mark.anyio
    async def test_stale_session_with_no_library_left_forgets_it(
        self, cache, unscoped_client, get_client
    ):
        request = _request(session_library_id="lib_old", stale=True)

        library_id = await _resolve_library_id(request, JWT, cache)

        assert library_id is None
        assert get_bound_library_id() is None
        cache.set_session_library.assert_awaited_once_with(SESSION_TOKEN, "lib_old", "")

    @pytest.mark.anyio
    async def test_undropped_library_is_not_served_unscoped(
        self, cache, unscoped_client, get_client
    ):
        """The session still records a library it can no longer use; serving
        the request unscoped would pair it with that library's sync state."""
        cache.set_session_library.return_value = False
        request = _request(session_library_id="lib_old", stale=True)

        with pytest.raises(HTTPException) as exc_info:
            await _resolve_library_id(request, JWT, cache)

        assert exc_info.value.status_code == 503
        assert get_bound_library_id() is None

    @pytest.mark.anyio
    async def test_session_scope_forgets_through_the_cache(
        self, cache, unscoped_client, get_client
    ):
        request = _request(session_library_id="lib_cached")
        await _resolve_library_id(request, JWT, cache)

        await forget_bound_library_if_gone(404, "Library lib_cached not found")

        # Conditional on the bound library, so a concurrent switch survives.
        cache.set_session_library.assert_awaited_once_with(
            SESSION_TOKEN, "lib_cached", ""
        )

    @pytest.mark.anyio
    async def test_api_key_scope_forgets_through_the_cache(
        self, cache, unscoped_client, get_client
    ):
        cache.get_for_api_key.return_value = "lib_cached"
        request = _request(jwt_token=API_KEY, session_token=None)
        await _resolve_library_id(request, API_KEY, cache)

        await forget_bound_library_if_gone(404, "Library lib_cached not found")

        cache.forget_api_key.assert_awaited_once_with(API_KEY)


class TestConcurrentResolution:
    """Requests that need a resolution at once share one."""

    @pytest.fixture
    def cache(self):
        cache = AsyncMock()
        cache.get_for_api_key.return_value = None
        cache.set_session_library.return_value = True
        return cache

    @pytest.fixture
    def release(self):
        return asyncio.Event()

    @pytest.fixture
    def unscoped_client(self, release):
        async def list_libraries():
            await release.wait()
            return [make_gumnut_library("lib_a", BASE)]

        client = Mock()
        client.libraries.list = AsyncMock(side_effect=list_libraries)
        client.users.me = AsyncMock(return_value=Mock(immich_library_id=None))
        with patch(
            "routers.utils.gumnut_client.get_gumnut_client",
            AsyncMock(return_value=client),
        ):
            yield client

    @staticmethod
    async def _start(cache, *requests: Mock, credential: str = JWT):
        tasks = [
            asyncio.create_task(_resolve_library_id(request, credential, cache))
            for request in requests
        ]
        await asyncio.sleep(0)  # let every request reach the shared resolution
        await asyncio.sleep(0)
        return tasks

    @staticmethod
    def _stale(**overrides) -> Mock:
        return _request(session_library_id="lib_a", stale=True, **overrides)

    @pytest.mark.anyio
    async def test_session_burst_fits_a_small_write_pool(
        self, unscoped_client, release
    ):
        """Keep the resolution joinable until its conditional write finishes."""
        entered, finish_write = asyncio.Event(), asyncio.Event()
        redis = fakeredis.FakeAsyncRedis(decode_responses=True)

        class BoundedRedis:
            active = 0
            peak = 0
            rejected = 0
            writes = 0

            async def eval(self, *args):
                if self.active == 2:
                    self.rejected += 1
                    raise MaxConnectionsError("Too many connections")
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.writes += 1
                entered.set()
                try:
                    await finish_write.wait()
                    return await redis.eval(*args)
                finally:
                    self.active -= 1

        pool = BoundedRedis()
        cache = LibraryCache(pool, SessionStore(pool))
        await redis.hset(
            f"session:{SESSION_TOKEN}",
            mapping={"library_id": "lib_a", "sync_epoch": "0", "client_epoch": "0"},
        )
        await redis.hset(f"session:{SESSION_TOKEN}:checkpoints", "AssetV1", "cursor")

        async def listing():
            await release.wait()
            return [make_gumnut_library(name, BASE) for name in ("lib_a", "lib_b")]

        unscoped_client.libraries.list.side_effect = listing
        unscoped_client.users.me.return_value = Mock(immich_library_id="lib_b")
        try:
            async with asyncio.timeout(5):
                tasks = await self._start(cache, *(self._stale() for _ in range(8)))
                release.set()
                await entered.wait()
                # A caller arriving during the Redis write joins the same owner.
                late = asyncio.create_task(
                    _resolve_library_id(self._stale(), JWT, cache)
                )
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                finish_write.set()
                assert await asyncio.gather(*tasks, late) == ["lib_b"] * 9
            assert (pool.peak, pool.rejected, pool.writes) == (1, 0, 1)
            unscoped_client.libraries.list.assert_awaited_once()
            assert await redis.hget(f"session:{SESSION_TOKEN}", "sync_epoch") == "1"
            assert not await redis.exists(f"session:{SESSION_TOKEN}:checkpoints")
        finally:
            finish_write.set()
            await redis.aclose()

    @pytest.mark.anyio
    async def test_stale_session_burst_resolves_once(
        self, cache, unscoped_client, release
    ):
        async with asyncio.timeout(5):
            tasks = await self._start(cache, *(self._stale() for _ in range(5)))
            release.set()
            assert await asyncio.gather(*tasks) == ["lib_a"] * 5

        unscoped_client.libraries.list.assert_awaited_once()
        unscoped_client.users.me.assert_awaited_once()
        cache.set_session_library.assert_awaited_once_with(
            SESSION_TOKEN, "lib_a", "lib_a", from_choice=False
        )

    @pytest.mark.anyio
    @pytest.mark.parametrize("difference", ["session", "snapshot", "provenance"])
    async def test_incompatible_session_reads_keep_separate_write_outcomes(
        self, cache, unscoped_client, release, difference
    ):
        first = self._stale(from_choice=True)
        second = self._stale(from_choice=True)
        if difference == "session":
            second.state.session_token = "another-session"
        elif difference == "snapshot":
            second.state.session_library_id = "lib_b"
        else:
            second.state.session_library_from_choice = False

        async def record(token, previous, library, **_kwargs):
            return token == SESSION_TOKEN and previous == "lib_a"

        cache.set_session_library.side_effect = record

        async def listing():
            await release.wait()
            return [make_gumnut_library("lib_new", BASE)]

        unscoped_client.libraries.list.side_effect = listing
        async with asyncio.timeout(5):
            tasks = await self._start(cache, first, second)
            release.set()
            results = await asyncio.gather(*tasks)
        assert results == [
            "lib_new",
            {"snapshot": "lib_b", "session": "lib_a", "provenance": "lib_new"}[
                difference
            ],
        ]
        assert unscoped_client.libraries.list.await_count == 2
        assert cache.set_session_library.await_count == 2

    @pytest.mark.anyio
    @pytest.mark.parametrize("previous", [None, "lib_a"])
    async def test_write_failure_reaches_waiters_and_a_later_burst_retries(
        self, cache, unscoped_client, release, previous
    ):
        cache.set_session_library.side_effect = [False, True]
        release.set()
        async with asyncio.timeout(5):
            tasks = await self._start(
                cache,
                *(_request(session_library_id=previous, stale=True) for _ in range(3)),
            )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if previous is None:
                assert all(
                    isinstance(result, HTTPException) and result.status_code == 503
                    for result in results
                )
            else:
                assert results == [previous] * 3
            assert (
                await _resolve_library_id(
                    _request(session_library_id=previous, stale=True), JWT, cache
                )
                == "lib_a"
            )
        assert cache.set_session_library.await_count == 2
        assert unscoped_client.libraries.list.await_count == 2

    @pytest.mark.anyio
    @pytest.mark.parametrize("no_choice", [False, True])
    async def test_session_clear_is_shared_for_rejected_or_missing_choices(
        self, cache, unscoped_client, release, no_choice
    ):
        async def listing():
            await release.wait()
            return (
                []
                if no_choice
                else [make_gumnut_library("lib_shared", BASE, role="collaborator")]
            )

        unscoped_client.libraries.list.side_effect = listing
        async with asyncio.timeout(5):
            tasks = await self._start(cache, *(self._stale() for _ in range(3)))
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        if no_choice:
            assert results == [None] * 3
        else:
            assert all(
                isinstance(result, HTTPException) and result.status_code == 403
                for result in results
            )
        cache.set_session_library.assert_awaited_once_with(SESSION_TOKEN, "lib_a", "")

    @pytest.mark.anyio
    async def test_cancelled_origin_during_persistence_leaves_one_shared_write(
        self, cache, unscoped_client, release
    ):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def record(*_args, **_kwargs):
            entered.set()
            await finish.wait()
            return True

        cache.set_session_library.side_effect = record
        async with asyncio.timeout(5):
            first, second = await self._start(cache, self._stale(), self._stale())
            release.set()
            await entered.wait()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            finish.set()
            assert await second == "lib_a"
        cache.set_session_library.assert_awaited_once()

    @pytest.mark.anyio
    @pytest.mark.parametrize("race", ["switch", "clear", "delete"])
    async def test_shared_write_cannot_overwrite_a_session_change(
        self, unscoped_client, release, race
    ):
        redis = fakeredis.FakeAsyncRedis(decode_responses=True)
        sessions = SessionStore(redis)
        cache = LibraryCache(redis, sessions)
        key = f"session:{SESSION_TOKEN}"
        await redis.hset(key, mapping={"library_id": "lib_a", "sync_epoch": "0"})
        unscoped_client.users.me.return_value = Mock(immich_library_id="lib_b")

        async def listing():
            await release.wait()
            return [make_gumnut_library(name, BASE) for name in ("lib_a", "lib_b")]

        unscoped_client.libraries.list.side_effect = listing
        try:
            async with asyncio.timeout(5):
                tasks = await self._start(cache, *(self._stale() for _ in range(3)))
                if race == "delete":
                    await redis.delete(key)
                else:
                    assert await sessions.set_library(
                        SESSION_TOKEN, "lib_a", "lib_c" if race == "switch" else ""
                    )
                release.set()
                assert await asyncio.gather(*tasks) == ["lib_a"] * 3
            if race == "delete":
                assert not await redis.exists(key)
            else:
                assert await redis.hget(key, "library_id") == (
                    "lib_c" if race == "switch" else ""
                )
                assert await redis.hget(key, "sync_epoch") == "1"
        finally:
            await redis.aclose()

    @pytest.mark.anyio
    async def test_api_key_burst_resolves_once(self, cache, unscoped_client, release):
        requests = [_request(session_token=None) for _ in range(5)]

        async with asyncio.timeout(5):
            tasks = await self._start(cache, *requests, credential=API_KEY)
            release.set()
            assert await asyncio.gather(*tasks) == ["lib_a"] * 5

        unscoped_client.libraries.list.assert_awaited_once()

    @pytest.mark.anyio
    async def test_failure_reaches_every_waiter_and_is_not_kept(
        self, cache, unscoped_client
    ):
        unscoped_client.libraries.list.side_effect = [
            make_sdk_status_error(500, "boom"),
            [make_gumnut_library("lib_a", BASE)],
        ]

        async with asyncio.timeout(5):
            tasks = await self._start(cache, *(self._stale() for _ in range(3)))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            retried = await _resolve_library_id(self._stale(), JWT, cache)

        assert all(isinstance(result, Exception) for result in results)
        assert retried == "lib_a"
        assert unscoped_client.libraries.list.await_count == 2

    @pytest.mark.anyio
    async def test_cancelled_request_leaves_the_others_their_result(
        self, cache, unscoped_client, release
    ):
        async with asyncio.timeout(5):
            first, second = await self._start(cache, self._stale(), self._stale())
            first.cancel()
            await asyncio.sleep(0)
            release.set()
            assert await second == "lib_a"

        assert first.cancelled()
        unscoped_client.libraries.list.assert_awaited_once()

    @pytest.mark.anyio
    async def test_failure_after_a_cancelled_request_is_not_reported_as_unhandled(
        self, cache, unscoped_client, release
    ):
        async def fail():
            await release.wait()
            raise make_sdk_status_error(500, "boom")

        unscoped_client.libraries.list.side_effect = fail
        unhandled = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: unhandled.append(context)
        )

        async with asyncio.timeout(5):
            first, second = await self._start(cache, self._stale(), self._stale())
            first.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(Exception, match="boom"):
                await second
            await asyncio.sleep(0)

        assert unhandled == []

    @pytest.mark.anyio
    async def test_different_credentials_resolve_separately(
        self, cache, unscoped_client, release
    ):
        release.set()

        async with asyncio.timeout(5):
            await asyncio.gather(
                _resolve_library_id(_request(session_library_id=None), "jwt-1", cache),
                _resolve_library_id(_request(session_library_id=None), "jwt-2", cache),
            )

        assert unscoped_client.libraries.list.await_count == 2

    @pytest.mark.anyio
    async def test_fallback_and_chosen_sessions_resolve_separately(
        self, cache, unscoped_client, release
    ):
        """A fallback session may stay on its library; a chosen one may not,
        so the two cannot share an outcome."""
        release.set()

        async with asyncio.timeout(5):
            await asyncio.gather(
                _resolve_library_id(self._stale(from_choice=False), JWT, cache),
                _resolve_library_id(self._stale(from_choice=True), JWT, cache),
            )

        assert unscoped_client.libraries.list.await_count == 2


class TestAuthenticatedClientDependency:
    @pytest.mark.anyio
    async def test_returns_client_bound_to_resolved_library(self):
        request = _request(session_library_id="lib_cached")
        bound_client = Mock()

        with patch(
            "routers.utils.gumnut_client.get_gumnut_client",
            AsyncMock(return_value=bound_client),
        ) as get_client:
            client = await get_authenticated_gumnut_client(request, AsyncMock())

        assert client is bound_client
        get_client.assert_awaited_once_with(JWT, "lib_cached")

    @pytest.mark.anyio
    async def test_missing_jwt_is_401_before_any_lookup(self):
        request = _request(jwt_token=None)
        cache = AsyncMock()

        with pytest.raises(Exception) as exc_info:
            await get_authenticated_gumnut_client(request, cache)

        assert getattr(exc_info.value, "status_code", None) == 401
        cache.get_for_api_key.assert_not_awaited()

    @pytest.mark.anyio
    async def test_current_library_id_reads_the_bound_scope(self):
        bind_library_scope(LibraryScope(library_id="lib_bound", forget=AsyncMock()))

        assert await get_current_library_id(Mock()) == "lib_bound"

    @pytest.mark.anyio
    async def test_current_library_id_is_none_when_unscoped(self):
        assert await get_current_library_id(Mock()) is None

    def test_library_lookup_failure_maps_through_the_error_handler(self):
        """The dependency now makes an upstream call on a cache miss; an SDK
        error there must reach the client Immich-shaped, not as a bare 500."""
        app = FastAPI()
        configure_exception_handlers(app)

        @app.middleware("http")
        async def fake_auth(request: Request, call_next):
            request.state.jwt_token = API_KEY
            request.state.session_token = None
            return await call_next(request)

        @app.get("/api/probe")
        async def probe(client: AsyncGumnut = Depends(get_authenticated_gumnut_client)):
            return {"ok": True}

        cache = AsyncMock()
        cache.get_for_api_key.return_value = None
        app.dependency_overrides[get_library_cache] = lambda: cache
        unscoped = Mock()
        unscoped.libraries.list = AsyncMock(
            side_effect=make_sdk_status_error(404, "nope", cls=NotFoundError)
        )

        with patch(
            "routers.utils.gumnut_client.get_gumnut_client",
            AsyncMock(return_value=unscoped),
        ):
            response = TestClient(app).get("/api/probe")

        assert response.status_code == 404
        assert response.json()["statusCode"] == 404


class TestBodyScopedRoutesDeclareTheDependency:
    """The unit tests call these handlers with an explicit `library_id`, so
    nothing else would notice the dependency wiring going missing."""

    @pytest.mark.parametrize(
        "handler",
        [create_album, create_person, create_face, create_stack, upload_asset],
    )
    def test_handler_declares_library_id_dependency(self, handler):
        default = inspect.signature(handler).parameters["library_id"].default
        assert default.dependency is get_current_library_id


class TestLibraryGoneHook:
    """The response hook drops the cached library on a library-not-found 404."""

    @pytest.fixture
    def forget(self):
        return AsyncMock()

    @pytest.fixture
    def scope(self, forget):
        scope = LibraryScope(library_id="lib_bound", forget=forget)
        bind_library_scope(scope)
        return scope

    @staticmethod
    def _response(status_code: int, body: object) -> httpx.Response:
        return httpx.Response(
            status_code,
            json=body,
            request=httpx.Request("GET", "http://gumnut.test/api/assets"),
        )

    @pytest.mark.anyio
    async def test_drops_library_when_the_bound_one_is_gone(self, scope, forget):
        await _response_hook(self._response(404, {"detail": LIBRARY_GONE}))

        forget.assert_awaited_once()
        assert scope.forgotten is True

    @pytest.mark.anyio
    async def test_drops_only_once_per_request(self, scope, forget):
        await _response_hook(self._response(404, {"detail": LIBRARY_GONE}))
        await _response_hook(self._response(404, {"detail": LIBRARY_GONE}))

        forget.assert_awaited_once()

    @pytest.mark.anyio
    async def test_ignores_other_entity_404(self, scope, forget):
        await _response_hook(self._response(404, {"detail": "Asset x not found"}))

        forget.assert_not_awaited()
        assert scope.forgotten is False

    @pytest.mark.anyio
    async def test_ignores_another_library(self, scope, forget):
        body = {"detail": "Library lib_other not found or not accessible by user u"}

        await _response_hook(self._response(404, body))

        forget.assert_not_awaited()

    @pytest.mark.anyio
    async def test_ignores_non_json_404(self, scope, forget):
        response = httpx.Response(
            404,
            text="not json",
            request=httpx.Request("GET", "http://gumnut.test/api/assets"),
        )

        await _response_hook(response)

        forget.assert_not_awaited()

    @pytest.mark.anyio
    async def test_no_scope_no_drop(self, forget):
        await _response_hook(self._response(404, {"detail": LIBRARY_GONE}))

        forget.assert_not_awaited()

    @pytest.mark.anyio
    async def test_cache_failure_does_not_escape(self, scope, forget):
        forget.side_effect = RuntimeError("redis exploded")

        await _response_hook(self._response(404, {"detail": LIBRARY_GONE}))

        assert scope.forgotten is True

    @pytest.mark.anyio
    async def test_still_captures_refresh_token(self, scope):
        response = httpx.Response(
            404,
            json={"detail": LIBRARY_GONE},
            headers={"x-new-access-token": "refreshed"},
            request=httpx.Request("GET", "http://gumnut.test/api/assets"),
        )

        await _response_hook(response)

        assert get_refreshed_token() == "refreshed"

    @pytest.mark.anyio
    async def test_round_trip_through_the_sdk(self, scope, forget):
        """Reading the body inside the hook must leave the SDK's own parsing
        intact: the call still raises NotFoundError with the body, and the
        drop fires once."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": LIBRARY_GONE})

        client = _mock_transport_client("lib_bound", httpx.MockTransport(handler))

        with pytest.raises(NotFoundError) as exc_info:
            await client.assets.list()

        assert exc_info.value.body == {"detail": LIBRARY_GONE}
        forget.assert_awaited_once()
