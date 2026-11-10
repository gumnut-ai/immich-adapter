"""Sessions are listed and managed by a public id, never by their token.

Drives the real auth middleware, sessions router, Socket.IO connect handler
and `SessionStore` on fakeredis, so a session's token and its public id are
exercised exactly as clients use them: the token as the credential, the id
only as a handle in the `/api/sessions` routes.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import fakeredis
import httpx
import pytest
from fastapi import FastAPI, Request

from config.exceptions import configure_exception_handlers
from config.telemetry import session_telemetry_id
from routers.api.sessions import router as sessions_router
from routers.middleware.auth_middleware import AuthMiddleware
from routers.utils.current_user import get_current_user_id
from services.session_store import SessionStore, get_session_store
from services.websockets import WebSocketEvent, connect, sio
from utils.jwt_encryption import encrypt_jwt

USER_ID = "770e8400-e29b-41d4-a716-446655440002"
OTHER_USER_ID = "880e8400-e29b-41d4-a716-446655440003"


@pytest.fixture
def store() -> SessionStore:
    return SessionStore(fakeredis.FakeAsyncRedis(decode_responses=True))


@pytest.fixture
async def client(store: SessionStore) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(sessions_router)
    configure_exception_handlers(app)

    async def session_user_id(request: Request) -> UUID:
        # In production the Gumnut API resolves the JWT's user; here the
        # session's own user stands in for it.
        session = await store.get_by_token(request.state.session_token)
        assert session is not None
        return UUID(session.user_id)

    async def get_store() -> SessionStore:
        return store

    app.dependency_overrides[get_current_user_id] = session_user_id
    app.dependency_overrides[get_session_store] = get_store
    with patch("routers.middleware.auth_middleware.get_session_store", get_store):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://adapter.test"
        ) as http:
            yield http


async def _login(store: SessionStore, user_id: str = USER_ID) -> str:
    """Create a session the way OAuth login does; return its access token."""
    session = await store.create(
        jwt_token="header.payload.signature",
        user_id=user_id,
        library_id="lib_1",
        device_type="iOS",
        device_os="iOS 17",
        app_version="1.94.0",
    )
    return str(session.token)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _listed_ids(http: httpx.AsyncClient, token: str) -> dict[str, bool]:
    """The caller's session list as {id: current}."""
    response = await http.get("/api/sessions", headers=_bearer(token))
    assert response.status_code == 200
    return {item["id"]: item["current"] for item in response.json()}


@pytest.mark.anyio
async def test_session_list_never_contains_a_token(client, store):
    first = await _login(store)
    second = await _login(store)

    response = await client.get("/api/sessions", headers=_bearer(second))

    assert response.status_code == 200
    assert first not in response.text
    assert second not in response.text
    listed = {item["id"]: item["current"] for item in response.json()}
    assert len(listed) == 2
    assert sorted(listed.values()) == [False, True]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "credential",
    [
        pytest.param(lambda value: {"headers": _bearer(value)}, id="bearer"),
        pytest.param(
            lambda value: {"headers": {"x-immich-user-token": value}},
            id="mobile-header",
        ),
        pytest.param(
            lambda value: {"headers": {"Cookie": f"immich_access_token={value}"}},
            id="web-cookie",
        ),
    ],
)
async def test_listed_session_ids_do_not_authenticate(client, store, credential):
    first = await _login(store)
    second = await _login(store)
    listed = await _listed_ids(client, second)

    for session_id in listed:
        response = await client.get("/api/sessions", **credential(session_id))
        assert response.status_code == 401

    # The tokens themselves still authenticate through the same channel.
    for token in (first, second):
        response = await client.get("/api/sessions", **credential(token))
        assert response.status_code == 200


@pytest.mark.anyio
async def test_listed_session_ids_do_not_open_a_websocket(client, store):
    token = await _login(store)
    (session_id,) = await _listed_ids(client, token)

    with (
        patch.object(sio, "enter_room", new_callable=AsyncMock),
        patch.object(sio, "emit", new_callable=AsyncMock),
        patch("services.websockets.get_session_store", AsyncMock(return_value=store)),
    ):
        listed_id_result = await connect(
            "sid-1", {"HTTP_X_IMMICH_USER_TOKEN": session_id}
        )
        token_result = await connect("sid-2", {"HTTP_X_IMMICH_USER_TOKEN": token})

    assert listed_id_result is False
    assert token_result is not False


@pytest.mark.anyio
async def test_revoking_other_sessions_keeps_the_callers_session(client, store):
    mine = await _login(store)
    other = await _login(store)

    with patch("routers.api.sessions.emit_session_delete", new_callable=AsyncMock):
        response = await client.delete("/api/sessions", headers=_bearer(mine))

    assert response.status_code == 204
    assert (
        await client.get("/api/sessions", headers=_bearer(other))
    ).status_code == 401
    listed = await _listed_ids(client, mine)
    assert list(listed.values()) == [True]


@pytest.mark.anyio
async def test_deleting_a_listed_id_revokes_that_session(client, store):
    mine = await _login(store)
    other = await _login(store)
    listed = await _listed_ids(client, mine)
    (other_id,) = [session_id for session_id, current in listed.items() if not current]

    joined_rooms: list[str] = []

    async def enter_room(sid: str, room: str) -> None:
        joined_rooms.append(room)

    with (
        patch.object(sio, "enter_room", side_effect=enter_room),
        patch.object(sio, "emit", new_callable=AsyncMock) as emit,
        patch("services.websockets.get_session_store", AsyncMock(return_value=store)),
    ):
        # The other device's websocket joins its session room.
        await connect("other-device", {"HTTP_X_IMMICH_USER_TOKEN": other})
        emit.reset_mock()

        response = await client.delete(
            f"/api/sessions/{other_id}", headers=_bearer(mine)
        )

    assert response.status_code == 204
    assert (
        await client.get("/api/sessions", headers=_bearer(other))
    ).status_code == 401
    assert (await client.get("/api/sessions", headers=_bearer(mine))).status_code == 200
    # The event reaches the room the other device joined, carrying its id.
    emit.assert_awaited_once_with(
        WebSocketEvent.SESSION_DELETE.value,
        other_id,
        room=f"session:{session_telemetry_id(other)}",
    )
    assert f"session:{session_telemetry_id(other)}" in joined_rooms


@pytest.mark.anyio
async def test_a_token_is_not_accepted_as_a_session_id(client, store):
    mine = await _login(store)
    other = await _login(store)

    deleted = await client.delete(f"/api/sessions/{other}", headers=_bearer(mine))
    updated = await client.put(
        f"/api/sessions/{other}",
        headers=_bearer(mine),
        json={"isPendingSyncReset": True},
    )

    assert deleted.status_code == 400
    assert updated.status_code == 400
    assert (
        await client.get("/api/sessions", headers=_bearer(other))
    ).status_code == 200


@pytest.mark.anyio
async def test_another_users_session_id_is_not_found(client, store):
    mine = await _login(store)
    theirs = await _login(store, user_id=OTHER_USER_ID)
    (their_id,) = await _listed_ids(client, theirs)

    deleted = await client.delete(f"/api/sessions/{their_id}", headers=_bearer(mine))
    updated = await client.put(
        f"/api/sessions/{their_id}",
        headers=_bearer(mine),
        json={"isPendingSyncReset": True},
    )

    assert deleted.status_code == 400
    assert updated.status_code == 400
    assert their_id not in await _listed_ids(client, mine)
    assert (
        await client.get("/api/sessions", headers=_bearer(theirs))
    ).status_code == 200


@pytest.mark.anyio
async def test_updating_a_listed_id_updates_that_session(client, store):
    token = await _login(store)
    (session_id,) = await _listed_ids(client, token)

    response = await client.put(
        f"/api/sessions/{session_id}",
        headers=_bearer(token),
        json={"isPendingSyncReset": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == session_id
    assert body["isPendingSyncReset"] is True
    assert token not in response.text


@pytest.mark.anyio
async def test_session_records_from_before_public_ids_keep_working(client, store):
    """A session hash in the exact format the adapter wrote before public ids
    needs no migration: its token still authenticates and it is listed by a
    public id."""
    mine = await _login(store)
    token = "3f2c1b7e-9a4d-4e5f-8b6a-1c2d3e4f5a6b"
    now = datetime.now(UTC).isoformat()
    redis = store._redis
    await redis.hset(
        f"session:{token}",
        mapping={
            "user_id": USER_ID,
            "library_id": "lib_1",
            "stored_jwt": encrypt_jwt("header.payload.signature"),
            "device_type": "Android",
            "device_os": "Android 14",
            "app_version": "1.94.0",
            "created_at": now,
            "updated_at": now,
            "sync_epoch": "0",
            "client_epoch": "0",
            "is_pending_sync_reset": "0",
            "library_checked_at": "0.0",
            "library_from_choice": "0",
        },
    )
    await redis.sadd(f"user:{USER_ID}:sessions", token)

    listed = await _listed_ids(client, token)

    assert token not in listed
    assert mine not in listed
    assert len(listed) == 2
    assert list(listed.values()).count(True) == 1


@pytest.mark.anyio
async def test_another_users_session_in_the_index_is_not_the_callers(store):
    """An index entry naming another user's session does not make it the
    caller's, for listing, revoking or lookup by id."""
    theirs = await store.get_by_token(await _login(store, user_id=OTHER_USER_ID))
    assert theirs is not None
    await store._redis.sadd(f"user:{USER_ID}:sessions", str(theirs.token))

    assert await store.get_by_user(USER_ID) == []
    assert await store.get_user_session(USER_ID, theirs.id) is None
    assert await store.get_user_session(OTHER_USER_ID, theirs.id) == theirs
