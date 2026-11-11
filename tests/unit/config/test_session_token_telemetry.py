"""Session bearer tokens never reach telemetry.

Each test drives real session-bearing code with Sentry initialized from the
production options (traces sampled at 100%) and a capturing transport, and
with a stdout handler built from the production logging config. It then
asserts the token appears nowhere in the serialized envelopes (transactions,
error events, logs) or in the formatted log output, tracebacks included.
"""

import logging
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import fakeredis
import pytest
import sentry_sdk

from config.telemetry import session_telemetry_id

# Importing the app applies the production logging config (non-propagating
# uvicorn loggers) at collection, whatever order the suite runs in.
from main import app
from routers.api.sync.routes import send_sync_ack
from routers.immich_models import SyncAckSetDto, SyncEntityType
from services.checkpoint_store import CheckpointStore
from services.session_store import SessionStore, SessionStoreError
from services.websockets import (
    WebSocketEvent,
    _sid_to_user,
    close_session_sockets,
    connect,
    emit_session_event,
    sio,
)


@pytest.fixture
def redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis(decode_responses=True)


async def _create_session(store: SessionStore) -> str:
    session = await store.create(
        jwt_token="header.payload.signature",
        user_id="user_1",
        library_id="lib_1",
        device_type="iOS",
        device_os="iOS 17",
        app_version="1.94.0",
    )
    return str(session.token)


@pytest.mark.anyio
async def test_session_cache_and_redis_spans_carry_no_token(telemetry, redis):
    store = SessionStore(redis)
    checkpoints = CheckpointStore(redis)

    with sentry_sdk.start_transaction(op="http.server", name="sync"):
        token = await _create_session(store)
        assert await store.get_by_token(token) is not None
        session_uuid = (await store.get_by_token(token)).token  # type: ignore[union-attr]
        assert await checkpoints.set_many(
            session_uuid, 0, [(SyncEntityType.AssetV1, "cursor")]
        )
        assert await checkpoints.get_all(session_uuid, 0)
        assert await store.update_activity(token)

    emitted = telemetry.flush()

    assert "transaction" in telemetry.item_types
    assert '"op": "cache.get"' in emitted or '"op":"cache.get"' in emitted
    assert "db.redis" in emitted
    assert f"session:{session_telemetry_id(token)}" in emitted
    assert token not in emitted


@pytest.mark.anyio
async def test_sync_ack_logs_carry_no_token(telemetry, redis):
    store = SessionStore(redis)
    token = await _create_session(store)
    request = Mock()
    request.state.session_token = token

    await send_sync_ack(
        request=SyncAckSetDto(acks=[]),
        http_request=request,
        checkpoint_store=CheckpointStore(redis),
        session_store=store,
    )
    # An ack from another sync epoch is dropped and logged separately.
    await send_sync_ack(
        request=SyncAckSetDto(acks=["AssetV1|cursor|99"]),
        http_request=request,
        checkpoint_store=CheckpointStore(redis),
        session_store=store,
    )

    emitted = telemetry.flush()

    assert "log" in telemetry.item_types
    assert "Acknowledged 0 checkpoints" in emitted
    assert "Dropped acks for a sync epoch" in emitted
    assert session_telemetry_id(token) in emitted
    assert token not in emitted
    assert telemetry.records_with(token) == []


@pytest.mark.anyio
async def test_websocket_lookup_failure_exception_carries_no_token(telemetry, redis):
    """A corrupt session's lookup error is logged with its traceback and
    stack locals; neither the message, the extras nor the locals may hold the
    token the client presented."""
    token = str(uuid4())
    await redis.hset(f"session:{token}", mapping={"user_id": "user_1"})
    environ = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    with patch(
        "services.websockets.get_session_store",
        AsyncMock(return_value=SessionStore(redis)),
    ):
        assert await connect("sid-1", environ) is False

    emitted = telemetry.flush()

    assert "event" in telemetry.item_types
    assert "SessionDataError" in telemetry.stdout.getvalue()
    assert session_telemetry_id(token) in emitted
    assert token not in emitted
    assert telemetry.records_with(token) == []


@pytest.mark.anyio
async def test_websocket_session_room_logs_carry_no_token(telemetry, redis):
    """Socket.IO logs room names at INFO, so a session's room must not be its
    token, and session events must still reach the room it joined."""
    store = SessionStore(redis)
    token = await _create_session(store)
    environ = {"HTTP_X_IMMICH_USER_TOKEN": token}

    with (
        patch(
            "services.websockets.get_session_store",
            AsyncMock(return_value=store),
        ),
        patch.object(sio.manager, "enter_room", AsyncMock()) as enter_room,
        patch.object(sio.manager, "emit", AsyncMock()) as manager_emit,
    ):
        assert await connect("sid-1", environ) is not False
        await emit_session_event(WebSocketEvent.SESSION_DELETE, token, token)

    emitted = telemetry.flush()

    joined_rooms = {call.args[2] for call in enter_room.await_args_list}
    session_room = manager_emit.await_args_list[-1].kwargs["room"]
    assert session_room in joined_rooms
    assert "is entering room" in emitted
    assert token not in emitted
    assert telemetry.records_with(token) == []
    assert token not in str(sorted(joined_rooms))


@pytest.mark.anyio
async def test_closing_a_sessions_sockets_logs_carry_no_token(telemetry, redis):
    """Deleting a session disconnects its sockets: python-socketio logs each
    server disconnect at INFO, and a failed disconnect is logged at WARNING
    with its traceback. None of them may hold the token."""
    store = SessionStore(redis)
    token = await _create_session(store)
    environ = {"HTTP_X_IMMICH_USER_TOKEN": token}
    real_disconnect = sio.disconnect
    failed: list[str] = []

    async def disconnect_failing_once(sid: str) -> None:
        if not failed:
            failed.append(sid)
            raise RuntimeError("transport closed")
        await real_disconnect(sid)

    with (
        patch(
            "services.websockets.get_session_store",
            AsyncMock(return_value=store),
        ),
        patch.object(sio.eio, "send", AsyncMock()),
        patch.object(sio.eio, "send_packet", AsyncMock()),
    ):
        try:
            for eio_sid in ("eio-1", "eio-2"):
                await sio._handle_eio_connect(eio_sid, environ)
                await sio._handle_eio_message(eio_sid, "0")
            with patch.object(sio, "disconnect", side_effect=disconnect_failing_once):
                await close_session_sockets(token)
        finally:
            for eio_sid in ("eio-1", "eio-2"):
                await sio._handle_eio_disconnect(eio_sid, sio.reason.CLIENT_DISCONNECT)
            _sid_to_user.clear()

    emitted = telemetry.flush()

    assert "Disconnecting" in emitted
    assert "Failed to disconnect a deleted session's socket" in emitted
    assert "RuntimeError" in telemetry.stdout.getvalue()
    assert token not in emitted
    assert telemetry.records_with(token) == []


def test_session_route_in_access_log_carries_no_token(telemetry):
    """The session-management routes put a session's token in the URL path,
    which the server access log records."""
    token = str(uuid4())

    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "192.0.2.1:50000",
        "PUT",
        f"/api/sessions/{token}",
        "1.1",
        200,
    )

    emitted = telemetry.flush()

    assert f"/api/sessions/{session_telemetry_id(token)}" in emitted
    assert token not in emitted


@pytest.mark.anyio
async def test_failed_session_request_carries_no_token(telemetry, redis):
    """An unhandled error on a session route: the request URL and headers, the
    transaction and the error event's stack locals (auth middleware, route
    resolution) all see the token."""
    from httpx import ASGITransport, AsyncClient

    from routers.utils.current_user import get_current_user_id

    store = SessionStore(redis)
    token = await _create_session(store)

    def failing_user_lookup() -> None:
        raise RuntimeError("user lookup failed")

    app.dependency_overrides[get_current_user_id] = failing_user_lookup
    try:
        with patch(
            "routers.middleware.auth_middleware.get_session_store",
            AsyncMock(return_value=store),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://testserver",
            ) as client:
                response = await client.put(
                    f"/api/sessions/{token}",
                    headers={"x-immich-user-token": token},
                    json={"isPendingSyncReset": True},
                )
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)

    emitted = telemetry.flush()

    assert response.status_code == 500
    assert {"event", "transaction"} <= telemetry.item_types
    assert "user lookup failed" in emitted
    assert f"/api/sessions/{session_telemetry_id(token)}" in emitted
    assert token not in emitted


@pytest.mark.anyio
async def test_redis_failure_exception_carries_no_token(telemetry, unreachable_redis):
    """redis-py's frames hold whole commands, where the token is a set member or
    a script argument with no `session:` prefix to recognize it by."""
    import redis.exceptions

    token = str(uuid4())
    store = SessionStore(unreachable_redis)

    for operation in (store.update_activity, store.delete_by_token):
        try:
            await operation(token)
        except redis.exceptions.RedisError, SessionStoreError:
            logging.getLogger(__name__).exception("Session write failed")
        else:
            raise AssertionError("expected the closed Redis port to fail")

    emitted = telemetry.flush()

    assert "event" in telemetry.item_types
    assert "Session write failed" in emitted
    assert token not in emitted
