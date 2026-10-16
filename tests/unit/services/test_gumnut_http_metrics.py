"""Exercise real SDK retries and the shared client's header-level boundary."""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import sentry_sdk
from gumnut import APIConnectionError, APITimeoutError, AsyncGumnut

from routers.utils import cdn_client, gumnut_client
from services import gumnut_http_client
from services.gumnut_http_client import GumnutHTTPClient


@pytest.fixture
def observations(monkeypatch):
    count = Mock()
    distribution = Mock()
    monkeypatch.setattr(sentry_sdk.metrics, "count", count)
    monkeypatch.setattr(sentry_sdk.metrics, "distribution", distribution)
    return count, distribution


def attempt_attributes(observations):
    count, _ = observations
    return [
        call.kwargs["attributes"]
        for call in count.call_args_list
        if call.args[0] == "gumnut.http.attempt"
    ]


@pytest.mark.anyio
async def test_real_sdk_status_retries_count_each_attempt(observations):
    statuses = iter([429, 503, 200])

    def respond(request):
        return httpx.Response(next(statuses), json={}, headers={"retry-after-ms": "1"})

    async with GumnutHTTPClient(transport=httpx.MockTransport(respond)) as http:
        sdk = AsyncGumnut(
            api_key="private-key", base_url="https://api.test", http_client=http
        )
        await sdk.users.me()

    assert attempt_attributes(observations) == [
        {
            "http.operation": "users",
            "http.status_class": status,
            "http.outcome": "response",
        }
        for status in ["4xx", "5xx", "2xx"]
    ]
    assert observations[1].call_count == 3
    assert all(
        call.kwargs["unit"] == "second" for call in observations[1].call_args_list
    )
    assert all(call.args[1] >= 0 for call in observations[1].call_args_list)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("exception_type", "sdk_error", "outcome"),
    [
        (httpx.ConnectError, APIConnectionError, "error"),
        (httpx.ReadTimeout, APITimeoutError, "timeout"),
    ],
)
async def test_real_sdk_transport_retries_and_terminal_failure(
    observations, monkeypatch, exception_type, sdk_error, outcome
):
    def fail(request):
        raise exception_type("private transport detail", request=request)

    async with GumnutHTTPClient(transport=httpx.MockTransport(fail)) as http:
        sdk = AsyncGumnut(
            api_key="private-key",
            base_url="https://api.test",
            max_retries=2,
            http_client=http,
        )
        monkeypatch.setattr(sdk, "_sleep_for_retry", AsyncMock())
        with pytest.raises(sdk_error):
            await sdk.users.me()

    assert (
        attempt_attributes(observations)
        == [
            {
                "http.operation": "users",
                "http.status_class": "none",
                "http.outcome": outcome,
            }
        ]
        * 3
    )
    assert observations[1].call_count == 3


@pytest.mark.anyio
async def test_transport_cancellation_propagates_once(observations):
    entered = asyncio.Event()

    async def wait(request):
        entered.set()
        return await asyncio.Future[httpx.Response]()

    async with GumnutHTTPClient(transport=httpx.MockTransport(wait)) as http:
        async with asyncio.timeout(2):
            task = asyncio.create_task(
                http.get("https://api.test/api/assets/private-id")
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert attempt_attributes(observations) == [
        {
            "http.operation": "assets",
            "http.status_class": "none",
            "http.outcome": "cancelled",
        }
    ]


@pytest.mark.anyio
async def test_redirect_hops_have_separate_header_observations(observations):
    def respond(request):
        if request.url.path.endswith("/me"):
            return httpx.Response(307, headers={"location": "/api/users/current"})
        return httpx.Response(200, json={})

    async with GumnutHTTPClient(transport=httpx.MockTransport(respond)) as http:
        response = await http.get(
            "https://api.test/api/users/me", follow_redirects=True
        )
    assert len(response.history) == 1
    assert [a["http.status_class"] for a in attempt_attributes(observations)] == [
        "3xx",
        "2xx",
    ]


class _BrokenBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise httpx.ReadTimeout("body timeout")
        yield b""  # pragma: no cover


@pytest.mark.anyio
async def test_body_failure_after_headers_is_not_second_attempt(observations):
    async with GumnutHTTPClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=_BrokenBody())
        )
    ) as http:
        with pytest.raises(httpx.ReadTimeout):
            await http.get("https://api.test/api/assets/private-id")
    assert len(attempt_attributes(observations)) == 1
    assert attempt_attributes(observations)[0]["http.outcome"] == "response"


@pytest.mark.anyio
async def test_hook_failures_preserve_errors_and_do_not_add_attempts(observations):
    async def fail_request(request):
        raise ValueError("request hook")

    async def fail_response(response):
        raise ValueError("response hook")

    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    async with GumnutHTTPClient(
        transport=transport, event_hooks={"request": [fail_request]}
    ) as http:
        with pytest.raises(ValueError, match="request hook"):
            await http.get("https://api.test/api/users/me")
    assert attempt_attributes(observations) == []
    async with GumnutHTTPClient(
        transport=transport, event_hooks={"response": [fail_response]}
    ) as http:
        with pytest.raises(ValueError, match="response hook"):
            await http.get("https://api.test/api/users/me")
    assert len(attempt_attributes(observations)) == 1
    assert attempt_attributes(observations)[0]["http.outcome"] == "response"


@pytest.mark.anyio
async def test_metric_failures_preserve_token_refresh_and_library_forget(monkeypatch):
    monkeypatch.setattr(
        sentry_sdk.metrics, "count", Mock(side_effect=RuntimeError("metrics down"))
    )
    monkeypatch.setattr(
        sentry_sdk.metrics,
        "distribution",
        Mock(side_effect=RuntimeError("metrics down")),
    )
    gumnut_client.init_request_scope()
    forget = AsyncMock()
    gumnut_client.bind_library_scope(gumnut_client.LibraryScope("lib_private", forget))

    def respond(request):
        return httpx.Response(
            404,
            headers={"x-new-access-token": "private-new-token"},
            json={"detail": "Library lib_private not found"},
        )

    async with GumnutHTTPClient(
        transport=httpx.MockTransport(respond),
        event_hooks={"response": [gumnut_client._response_hook]},
    ) as http:
        response = await http.get("https://api.test/api/assets/private-id")
    assert response.status_code == 404
    assert gumnut_client.get_refreshed_token() == "private-new-token"
    forget.assert_awaited_once()


@pytest.mark.anyio
async def test_attributes_are_bounded_and_cdn_client_is_excluded(
    observations, monkeypatch
):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with GumnutHTTPClient(transport=transport) as http:
        await http.get(
            "https://private-host/api/assets/private-id?library_id=lib_private&signature=secret",
            headers={"Authorization": "Bearer secret"},
        )
        await http.get("https://private-host/private-path/private-id")
    async with httpx.AsyncClient(transport=transport) as cdn:
        monkeypatch.setattr(cdn_client, "_cdn_http_client", cdn)
        response = await cdn_client.open_cdn_response(
            "https://cdn.test/private-id?signature=secret"
        )
        await response.aclose()
    assert attempt_attributes(observations) == [
        {"http.operation": op, "http.status_class": "2xx", "http.outcome": "response"}
        for op in ["assets", "other"]
    ]
    assert observations[1].call_count == 2


@pytest.mark.anyio
async def test_shared_api_client_installs_observer(monkeypatch):
    monkeypatch.setattr(gumnut_client, "_shared_http_client", None)
    http = await gumnut_client.get_shared_http_client()
    try:
        assert isinstance(http, GumnutHTTPClient)
        assert http.event_hooks["response"][1] is gumnut_client._response_hook
        assert http.timeout == httpx.Timeout(30)
    finally:
        await gumnut_client.close_shared_http_client()


@pytest.mark.anyio
async def test_duration_stops_before_existing_hook_and_body(observations, monkeypatch):
    now = 1.0
    monkeypatch.setattr(gumnut_http_client.time, "perf_counter", lambda: now)

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal now
            now = 15.0
            yield b"body"

    def respond(request):
        nonlocal now
        now = 3.0
        return httpx.Response(200, stream=Body())

    async def existing_hook(response):
        nonlocal now
        now = 9.0

    async with GumnutHTTPClient(
        transport=httpx.MockTransport(respond),
        event_hooks={"response": [existing_hook]},
    ) as http:
        response = await http.get("https://api.test/api/assets/private-id")
    assert response.content == b"body"
    observations[1].assert_called_once_with(
        "gumnut.http.attempt.duration",
        2.0,
        unit="second",
        attributes={
            "http.operation": "assets",
            "http.status_class": "2xx",
            "http.outcome": "response",
        },
    )


@pytest.mark.anyio
async def test_concurrent_sends_keep_their_own_attempt_state(observations):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def respond(request):
        if request.url.path == "/api/assets/private-id":
            entered.set()
            await release.wait()
            return httpx.Response(200)
        raise httpx.ConnectError("connection failed", request=request)

    async with GumnutHTTPClient(transport=httpx.MockTransport(respond)) as http:
        async with asyncio.timeout(2):
            asset = asyncio.create_task(
                http.get("https://api.test/api/assets/private-id")
            )
            await entered.wait()
            try:
                with pytest.raises(httpx.ConnectError):
                    await http.get("https://api.test/api/libraries")
            finally:
                release.set()
                await asset
    assert attempt_attributes(observations) == [
        {
            "http.operation": "libraries",
            "http.status_class": "none",
            "http.outcome": "error",
        },
        {
            "http.operation": "assets",
            "http.status_class": "2xx",
            "http.outcome": "response",
        },
    ]
