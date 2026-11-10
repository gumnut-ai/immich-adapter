"""Deleting a session ends its live Socket.IO connections.

Drives the real auth middleware, logout and sessions routers, Socket.IO
connect handler and room manager, and `SessionStore` on fakeredis. Only the
engine.io transport is replaced, by a recorder of the Socket.IO packets each
connection is sent, so room membership, emission and disconnection are the
server's own.
"""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import fakeredis
import httpx
import pytest
from fastapi import FastAPI, Request
from socketio import packet

from config.exceptions import configure_exception_handlers
from routers.api.auth import router as auth_router
from routers.api.sessions import router as sessions_router
from routers.middleware.auth_middleware import AuthMiddleware
from routers.utils.current_user import get_current_user_id
from routers.utils.gumnut_client import get_authenticated_gumnut_client_optional
from services.session_store import SessionStore, get_session_store, public_session_id
from services.websockets import (
    WebSocketEvent,
    _sid_to_user,
    close_session_sockets,
    emit_user_event,
    sio,
)

USER_ID = "770e8400-e29b-41d4-a716-446655440002"


@pytest.fixture
def store() -> SessionStore:
    return SessionStore(fakeredis.FakeAsyncRedis(decode_responses=True))


@pytest.fixture
async def client(store: SessionStore) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(auth_router)
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

    async def no_gumnut_client() -> None:
        return None

    app.dependency_overrides[get_current_user_id] = session_user_id
    app.dependency_overrides[get_session_store] = get_store
    app.dependency_overrides[get_authenticated_gumnut_client_optional] = (
        no_gumnut_client
    )
    with patch("routers.middleware.auth_middleware.get_session_store", get_store):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://adapter.test"
        ) as http:
            yield http


class Wire:
    """Socket.IO packets sent to each engine.io connection, in order."""

    def __init__(self) -> None:
        # (Socket.IO packet type, decoded data) per engine.io connection.
        self.sent: dict[str, list[tuple[int, Any]]] = {}
        self.opened: list[str] = []

    async def send(self, eio_sid: str, data: Any) -> None:
        pkt = packet.Packet(encoded_packet=data)
        self.sent.setdefault(eio_sid, []).append((pkt.packet_type, pkt.data))

    async def send_packet(self, eio_sid: str, eio_pkt: Any) -> None:
        # Room emits encode once and hand engine.io a ready message packet.
        await self.send(eio_sid, eio_pkt.data)

    async def open(self, eio_sid: str, token: str) -> None:
        """Open a connection the way the mobile client does, and assert the
        server accepted it."""
        self.opened.append(eio_sid)
        await sio._handle_eio_connect(eio_sid, {"HTTP_X_IMMICH_USER_TOKEN": token})
        await sio._handle_eio_message(eio_sid, "0")
        assert self.sent[eio_sid][-1][0] == packet.CONNECT

    def events(self, eio_sid: str) -> list[Any]:
        """Event names, and `DISCONNECT`, sent to a connection."""
        return [
            data[0] if packet_type == packet.EVENT else packet_type
            for packet_type, data in self.sent.get(eio_sid, [])
            if packet_type in (packet.EVENT, packet.DISCONNECT)
        ]

    def mark(self) -> None:
        self.sent.clear()


@pytest.fixture
async def wire(store: SessionStore) -> AsyncIterator[Wire]:
    recorder = Wire()
    with (
        patch.object(sio.eio, "send", side_effect=recorder.send),
        patch.object(sio.eio, "send_packet", side_effect=recorder.send_packet),
        patch("services.websockets.get_session_store", AsyncMock(return_value=store)),
    ):
        yield recorder
        # The room manager is process-global: close every connection, even
        # after a failed test, so later tests start with empty rooms.
        for eio_sid in recorder.opened:
            await sio._handle_eio_disconnect(eio_sid, sio.reason.CLIENT_DISCONNECT)
    _sid_to_user.clear()


async def _login(store: SessionStore) -> str:
    """Create a session the way OAuth login does; return its access token."""
    session = await store.create(
        jwt_token="header.payload.signature",
        user_id=USER_ID,
        library_id="lib_1",
        device_type="iOS",
        device_os="iOS 17",
        app_version="1.94.0",
    )
    return str(session.token)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _emit_user_event() -> None:
    await emit_user_event(WebSocketEvent.ASSET_TRASH, USER_ID, ["asset-1"])


TRASH = WebSocketEvent.ASSET_TRASH.value
SESSION_DELETE = WebSocketEvent.SESSION_DELETE.value


async def _assert_user_events_reach(wire: Wire, *eio_sids: str) -> None:
    wire.mark()
    await _emit_user_event()
    for eio_sid in eio_sids:
        assert wire.events(eio_sid) == [TRASH]


@pytest.mark.anyio
async def test_deleting_a_session_disconnects_all_of_its_sockets(client, store, wire):
    mine = await _login(store)
    other = await _login(store)
    await wire.open("mine", mine)
    await wire.open("other-1", other)
    await wire.open("other-2", other)
    await _assert_user_events_reach(wire, "mine", "other-1", "other-2")

    wire.mark()
    response = await client.delete(
        f"/api/sessions/{public_session_id(other)}", headers=_bearer(mine)
    )

    assert response.status_code == 204
    # The client is told first, so web can navigate to its logout screen.
    for eio_sid in ("other-1", "other-2"):
        assert wire.events(eio_sid) == [SESSION_DELETE, packet.DISCONNECT]
        assert wire.sent[eio_sid][0][1] == [
            SESSION_DELETE,
            str(public_session_id(other)),
        ]
    assert wire.events("mine") == []

    wire.mark()
    await _emit_user_event()
    assert wire.events("mine") == [TRASH]
    assert wire.events("other-1") == []
    assert wire.events("other-2") == []

    # Reconnecting with the deleted token is refused.
    await sio._handle_eio_message("other-1", "0")
    assert wire.sent["other-1"][-1][0] == packet.CONNECT_ERROR


@pytest.mark.anyio
async def test_deleting_other_sessions_keeps_the_callers_socket(client, store, wire):
    mine = await _login(store)
    second = await _login(store)
    third = await _login(store)
    await wire.open("mine", mine)
    await wire.open("second", second)
    await wire.open("third", third)
    await _assert_user_events_reach(wire, "mine", "second", "third")

    wire.mark()
    response = await client.delete("/api/sessions", headers=_bearer(mine))

    assert response.status_code == 204
    assert wire.events("second") == [SESSION_DELETE, packet.DISCONNECT]
    assert wire.events("third") == [SESSION_DELETE, packet.DISCONNECT]
    assert wire.events("mine") == []

    wire.mark()
    await _emit_user_event()
    assert wire.events("mine") == [TRASH]
    assert wire.events("second") == []
    assert wire.events("third") == []


@pytest.mark.anyio
async def test_logout_disconnects_the_sessions_sockets(client, store, wire):
    mine = await _login(store)
    other = await _login(store)
    await wire.open("mine", mine)
    await wire.open("other", other)
    await _assert_user_events_reach(wire, "mine", "other")

    wire.mark()
    response = await client.post("/api/auth/logout", headers=_bearer(other))

    assert response.status_code == 200
    assert wire.events("other") == [SESSION_DELETE, packet.DISCONNECT]
    assert wire.events("mine") == []

    wire.mark()
    await _emit_user_event()
    assert wire.events("mine") == [TRASH]
    assert wire.events("other") == []


@pytest.mark.anyio
async def test_a_failed_disconnect_does_not_stop_the_others(store, wire, caplog):
    token = await _login(store)
    await wire.open("first", token)
    await wire.open("second", token)

    real_disconnect = sio.disconnect
    calls: list[str] = []

    async def flaky_disconnect(sid: str, *args: Any, **kwargs: Any) -> None:
        calls.append(sid)
        if len(calls) == 1:
            raise RuntimeError("transport closed")
        await real_disconnect(sid, *args, **kwargs)

    with patch.object(sio, "disconnect", side_effect=flaky_disconnect):
        await close_session_sockets(token)

    assert len(calls) == 2
    disconnected = [
        eio_sid
        for eio_sid in ("first", "second")
        if wire.events(eio_sid)[-1] == packet.DISCONNECT
    ]
    assert len(disconnected) == 1
    assert "Failed to disconnect a deleted session's socket" in caplog.text
    assert token not in caplog.text
