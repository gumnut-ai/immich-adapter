"""The PKCE code verifier the adapter generates never reaches telemetry.

Drives web OAuth sign-in with Sentry initialized from the production options
and a capturing transport (the `telemetry` fixture of the session-token
telemetry tests), then asserts the verifier appears nowhere in the serialized
envelopes, the formatted log output or the log records.
"""

import socket
from unittest.mock import AsyncMock, Mock, patch

import fakeredis
import pytest
import redis.asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from main import app
from routers.utils.gumnut_client import get_unauthenticated_gumnut_client
from routers.utils.oauth_utils import generate_pkce_pair
from services.oauth_sign_in_store import OAuthSignInStore, get_oauth_sign_in_store
from services.session_store import get_session_store
from tests.unit.config.test_session_token_telemetry import (  # noqa: F401
    _records_with,
    telemetry,
)

WEB_CALLBACK = "https://testserver/auth/login"


@pytest.fixture
def pkce_pair():
    """A real PKCE pair, also handed to the router so the test knows it."""
    pair = generate_pkce_pair()
    with patch("routers.api.oauth.generate_pkce_pair", return_value=pair):
        yield pair


@pytest.fixture
def backend():
    client = Mock()
    auth_url = Mock()
    auth_url.url = "https://idp.example.com/authorize?state=server-state"
    client.oauth.auth_url = AsyncMock(return_value=auth_url)
    client.oauth.exchange = AsyncMock(side_effect=RuntimeError("exchange failed"))
    return client


async def _sign_in(store: OAuthSignInStore, backend: Mock) -> list[int]:
    app.dependency_overrides[get_unauthenticated_gumnut_client] = lambda: backend
    app.dependency_overrides[get_oauth_sign_in_store] = lambda: store
    app.dependency_overrides[get_session_store] = lambda: AsyncMock()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://testserver",
        ) as client:
            authorize = await client.post(
                "/api/oauth/authorize", json={"redirectUri": WEB_CALLBACK}
            )
            if authorize.status_code != 201:
                return [authorize.status_code]
            callback = await client.post(
                "/api/oauth/callback",
                json={"url": f"{WEB_CALLBACK}?code=abc123&state=server-state"},
            )
            return [authorize.status_code, callback.status_code]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_failed_exchange_carries_no_verifier(telemetry, pkce_pair, backend):  # noqa: F811
    """The exchange fails after the verifier was stored in Redis, read back and
    handed to the Gumnut API client: the Redis spans, the request
    transactions, and the error event with its stack locals all saw it."""
    verifier, _ = pkce_pair
    store = OAuthSignInStore(fakeredis.FakeAsyncRedis(decode_responses=True))

    statuses = await _sign_in(store, backend)

    emitted = telemetry.flush()

    assert statuses == [201, 500]
    assert backend.oauth.exchange.call_args.kwargs["code_verifier"] == verifier
    assert {"event", "transaction"} <= telemetry.item_types
    assert "db.redis" in emitted
    assert "Failed to complete OAuth authentication" in emitted
    assert verifier not in emitted
    assert _records_with(verifier) == []


@pytest.mark.anyio
async def test_redis_failure_on_authorize_carries_no_verifier(
    telemetry,  # noqa: F811
    pkce_pair,
    backend,
):
    """Storing the sign-in fails inside the store, whose frame holds the
    verifier as a local."""
    verifier, _ = pkce_pair
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    client = redis.asyncio.Redis(
        port=closed_port,
        socket_connect_timeout=0.2,
        retry=Retry(NoBackoff(), retries=0),
        decode_responses=True,
    )

    statuses = await _sign_in(OAuthSignInStore(client), backend)
    await client.aclose()

    emitted = telemetry.flush()

    assert statuses == [500]
    assert "event" in telemetry.item_types
    assert "Failed to get OAuth authorization URL" in emitted
    assert verifier not in emitted
    assert _records_with(verifier) == []
