"""Library selection may replace one user read only in its originating request."""

import asyncio
import base64
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
import shortuuid
from uuid import UUID
from fastapi import Request
from gumnut import AuthenticationError, PermissionDeniedError
from gumnut.types.user_response import UserResponse

from routers.utils.current_user import get_current_user_admin
from routers.utils.gumnut_client import (
    _library_choices_in_flight,
    _resolve_library_id,
    get_raw_current_user,
    init_request_scope,
    set_refreshed_token,
)
from tests.conftest import make_gumnut_library, make_sdk_status_error


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return f"header.{payload}.signature"


def request(credential, *, cached=False, method="GET", path="/api/users/me"):
    req = Request({"type": "http", "method": method, "path": path, "headers": []})
    req.state.jwt_token = credential
    req.state.session_token = None if credential.startswith("apikey_") else "session"
    req.state.session_library_id = "lib_old" if cached else None
    req.state.session_library_checked_at = 0
    req.state.session_library_from_choice = False
    return req


@pytest.fixture(autouse=True)
def clean_scope():
    init_request_scope()
    _library_choices_in_flight.clear()


@pytest.fixture
def client():
    now = datetime.now(timezone.utc)
    user = UserResponse.model_construct(
        id="intuser_" + shortuuid.encode(UUID("11111111-1111-4111-8111-111111111111")),
        immich_library_id=None,
        email="user@example.com",
        first_name="Example",
        last_name="User",
        is_active=True,
        created_at=now,
        updated_at=now,
        storage_limit_bytes=100,
        storage_used_bytes=10,
    )
    client = Mock()
    client.api_key = jwt(time.time() + 3600)
    client.libraries.list = AsyncMock(
        return_value=[make_gumnut_library("lib_old", now)]
    )
    client.users.me = AsyncMock(return_value=user)
    return client


@pytest.fixture
def cache():
    cache = AsyncMock()
    cache.get_for_api_key.return_value = None
    cache.set_session_library.return_value = True
    return cache


async def resolve(req, client, cache):
    with patch(
        "routers.utils.gumnut_client.get_gumnut_client", AsyncMock(return_value=client)
    ):
        return await _resolve_library_id(req, req.state.jwt_token, cache)


@pytest.mark.anyio
@pytest.mark.parametrize("cached", [False, True])
async def test_initial_and_recheck_user_read_reused_once(client, cache, cached):
    req = request(client.api_key, cached=cached)
    await resolve(req, client, cache)
    user = await get_raw_current_user(req, client)
    assert user.id == client.users.me.return_value.id
    assert user is not client.users.me.return_value
    user.first_name = "Changed"
    assert client.users.me.return_value.first_name == "Example"
    client.users.me.assert_awaited_once()
    await get_raw_current_user(req, client)
    assert client.users.me.await_count == 2
    # Another request never gets the originating request's seed.
    await get_raw_current_user(request(client.api_key), client)
    assert client.users.me.await_count == 3


@pytest.mark.anyio
async def test_fresh_library_still_needs_live_user(client, cache):
    req = request(client.api_key, cached=True)
    req.state.session_library_checked_at = time.time()
    await resolve(req, client, cache)
    client.users.me.assert_not_awaited()
    await get_raw_current_user(req, client)
    client.users.me.assert_awaited_once()


@pytest.mark.anyio
async def test_coalesced_waiter_reads_its_own_user(client, cache):
    entered, release = asyncio.Event(), asyncio.Event()

    async def libraries():
        entered.set()
        await release.wait()
        return [make_gumnut_library("lib_old", datetime.now(timezone.utc))]

    client.libraries.list.side_effect = libraries
    owner, waiter = request(client.api_key), request(client.api_key)

    async def run(req):
        init_request_scope()
        await _resolve_library_id(req, client.api_key, cache)
        return await get_raw_current_user(req, client)

    with patch(
        "routers.utils.gumnut_client.get_gumnut_client", AsyncMock(return_value=client)
    ):
        async with asyncio.timeout(5):
            first = asyncio.create_task(run(owner))
            await entered.wait()
            second = asyncio.create_task(run(waiter))
            await asyncio.sleep(0)
            release.set()
            users = await asyncio.gather(first, second)
    assert users[0] is not users[1]
    client.libraries.list.assert_awaited_once()
    assert client.users.me.await_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change", ["refresh_during", "refresh_after", "credential", "expiry", "unknown"]
)
async def test_uncertain_or_changed_credentials_stay_live(client, cache, change):
    req = request(client.api_key)
    if change == "refresh_during":

        async def me():
            set_refreshed_token("refreshed")
            return client.users.me.return_value

        client.users.me.side_effect = me
    await resolve(req, client, cache)
    if change == "refresh_after":
        set_refreshed_token("refreshed")
    elif change == "credential":
        req.state.jwt_token = jwt(time.time() + 7200)
    elif change == "expiry":
        with patch(
            "routers.utils.thumbnail_cache.time.time", return_value=time.time() + 7200
        ):
            await get_raw_current_user(req, client)
        assert client.users.me.await_count == 2
        return
    elif change == "unknown":
        # A successful read with an unparseable token is never a freshness proof.
        req = request("unknown")
        client.api_key = "unknown"
        await resolve(req, client, cache)
    await get_raw_current_user(req, client)
    assert client.users.me.await_count == (3 if change == "unknown" else 2)


@pytest.mark.anyio
async def test_expired_seed_auth_failure_is_raised_before_stream(client, cache):
    req = request(client.api_key, method="POST", path="/api/sync/stream")
    await resolve(req, client, cache)
    client.users.me.side_effect = make_sdk_status_error(
        401, "expired", cls=AuthenticationError
    )
    with patch(
        "routers.utils.thumbnail_cache.time.time", return_value=time.time() + 7200
    ):
        with pytest.raises(AuthenticationError):
            await get_raw_current_user(req, client)
    assert client.users.me.await_count == 2


@pytest.mark.anyio
async def test_permission_denied_selection_caches_no_user(client, cache):
    req = request(client.api_key)
    client.users.me.side_effect = [
        make_sdk_status_error(403, "denied", cls=PermissionDeniedError),
        client.users.me.return_value,
    ]
    assert await resolve(req, client, cache) == "lib_old"
    await get_raw_current_user(req, client)
    assert client.users.me.await_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "method,path,dependency,reused",
    [
        ("POST", "/api/assets", True, True),
        ("POST", "/api/assets", False, False),
        ("PUT", "/api/users/me", True, False),
        ("POST", "/api/albums", True, False),
        ("POST", "/api/sync/stream", False, True),
    ],
)
async def test_mutation_boundary(client, cache, method, path, dependency, reused):
    req = request(client.api_key, method=method, path=path)
    await resolve(req, client, cache)
    if dependency:
        await get_current_user_admin(req, client)
    else:
        await get_raw_current_user(req, client)
    assert client.users.me.await_count == (1 if reused else 2)


@pytest.mark.anyio
async def test_api_key_fallback_and_library_move_writes_are_preserved(client, cache):
    client.api_key = "apikey_example"
    req = request(client.api_key)
    await resolve(req, client, cache)
    await get_raw_current_user(req, client)
    client.users.me.assert_awaited_once()
    cache.remember_for_api_key.assert_awaited_once_with(client.api_key, "lib_old")
    client.api_key = jwt(time.time() + 3600)
    req = request(client.api_key, cached=True)
    client.users.me.return_value.immich_library_id = "lib_new"
    client.libraries.list.return_value.append(
        make_gumnut_library("lib_new", datetime.now(timezone.utc))
    )
    assert await resolve(req, client, cache) == "lib_new"
    cache.set_session_library.assert_awaited_with(
        "session", "lib_old", "lib_new", from_choice=True
    )


@pytest.mark.anyio
async def test_unscoped_selection_leaves_user_read_live(client, cache):
    client.api_key = "apikey_example"
    req = request(client.api_key)
    client.libraries.list.return_value = []
    assert await resolve(req, client, cache) is None
    client.users.me.assert_not_awaited()
    await get_raw_current_user(req, client)
    client.users.me.assert_awaited_once()
    cache.remember_for_api_key.assert_not_awaited()


@pytest.mark.anyio
async def test_scoped_selection_user_read_reused_once(client, cache):
    client.api_key = "apikey_example"
    req = request(client.api_key)
    client.libraries.list.side_effect = make_sdk_status_error(
        403, "restricted", cls=PermissionDeniedError
    )
    client.users.me.return_value.immich_library_id = "lib_old"
    client.libraries.retrieve = AsyncMock(
        return_value=make_gumnut_library("lib_old", datetime.now(timezone.utc))
    )
    assert await resolve(req, client, cache) == "lib_old"
    user = await get_raw_current_user(req, client)
    assert user.id == client.users.me.return_value.id
    client.users.me.assert_awaited_once()
    await get_raw_current_user(req, client)
    assert client.users.me.await_count == 2


@pytest.mark.anyio
async def test_failed_selection_user_is_not_retained(client, cache):
    req = request(client.api_key)
    client.users.me.side_effect = [
        make_sdk_status_error(401, "expired", cls=AuthenticationError),
        client.users.me.return_value,
    ]
    with pytest.raises(AuthenticationError):
        await resolve(req, client, cache)
    assert getattr(req.state, "library_selection_user", None) is None
    await get_raw_current_user(req, client)
    assert client.users.me.await_count == 2
