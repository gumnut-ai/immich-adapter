import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sentry_sdk import metrics
from starlette.middleware.base import BaseHTTPMiddleware

from routers.middleware.stream_metrics_middleware import StreamMetricsMiddleware


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, "body", "final_send", "cancel"])
async def test_completion_requires_final_body_send_and_preserves_failures(
    monkeypatch, failure
):
    count = Mock()
    monkeypatch.setattr(metrics, "count", count)
    scope = {
        "type": "http",
        "method": "GET",
        "route": SimpleNamespace(path="/api/assets/{id}/thumbnail"),
    }
    messages = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"one", "more_body": True})
        assert count.call_count == 0
        if failure == "body":
            raise ValueError("body failed")
        if failure == "cancel":
            raise asyncio.CancelledError()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def send(message):
        if failure == "final_send" and message.get("more_body") is False:
            raise OSError("disconnected")
        messages.append(message)

    middleware = StreamMetricsMiddleware(app)
    if failure:
        error = {
            "body": ValueError,
            "cancel": asyncio.CancelledError,
            "final_send": OSError,
        }[failure]
        with pytest.raises(error):
            await middleware(scope, AsyncMock(), send)
    else:
        await middleware(scope, AsyncMock(), send)
        assert messages[-1]["more_body"] is False
    count.assert_called_once_with(
        "thumbnail.delivery",
        1,
        attributes={"stream.outcome": "abort" if failure else "complete"},
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state,complete,outcome",
    [
        ({"sync_stream_complete": True}, True, "complete"),
        ({"sync_stream_complete": True}, False, "abort"),
        ({"sync_stream_reset": True}, True, "reset"),
        ({}, True, "abort"),
    ],
)
async def test_sync_requires_completion_marker_and_delivered_body(
    monkeypatch, state, complete, outcome
):
    count = Mock()
    monkeypatch.setattr(metrics, "count", count)
    scope = {
        "type": "http",
        "method": "POST",
        "state": state,
        "route": SimpleNamespace(path="/api/sync/stream"),
    }

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        if complete:
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    await StreamMetricsMiddleware(app)(scope, AsyncMock(), AsyncMock())
    count.assert_called_once_with(
        "sync.stream", 1, attributes={"stream.outcome": outcome}
    )


def test_real_middleware_layers_preserve_bytes_headers_and_measure_outer_send(
    monkeypatch,
):
    count = Mock()
    monkeypatch.setattr(metrics, "count", count)
    app = FastAPI()

    async def passthrough(request, call_next):
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
    app.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
    app.add_middleware(StreamMetricsMiddleware)

    @app.get("/api/assets/{id}/thumbnail")
    async def thumbnail(id: str):
        async def body():
            yield b"one"
            yield b"two"

        return StreamingResponse(
            body(), media_type="image/webp", headers={"ETag": "example"}
        )

    with TestClient(app) as client:
        response = client.get("/api/assets/private-id/thumbnail?c=private-value")
    assert response.content == b"onetwo"
    assert response.headers["etag"] == "example"
    assert response.headers["content-type"] == "image/webp"
    count.assert_called_once_with(
        "thumbnail.delivery", 1, attributes={"stream.outcome": "complete"}
    )


@pytest.mark.anyio
async def test_other_routes_and_rejected_requests_are_outside_stream_denominator(
    monkeypatch,
):
    count = Mock()
    monkeypatch.setattr(metrics, "count", count)
    for route, status in [("/other", 200), ("/api/assets/{id}/thumbnail", 401)]:

        async def app(scope, receive, send, status=status):
            await send({"type": "http.response.start", "status": status})
            await send({"type": "http.response.body", "body": b""})

        await StreamMetricsMiddleware(app)(
            {"type": "http", "method": "GET", "route": SimpleNamespace(path=route)},
            AsyncMock(),
            AsyncMock(),
        )
    count.assert_not_called()


@pytest.mark.anyio
async def test_metric_failure_does_not_change_completed_response(monkeypatch):
    monkeypatch.setattr(
        metrics, "count", Mock(side_effect=ValueError("metrics failed"))
    )
    send = AsyncMock()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    await StreamMetricsMiddleware(app)(
        {
            "type": "http",
            "method": "GET",
            "route": SimpleNamespace(path="/api/assets/{id}/thumbnail"),
        },
        AsyncMock(),
        send,
    )
    assert send.await_args is not None
    assert send.await_args.args[0]["body"] == b"ok"
