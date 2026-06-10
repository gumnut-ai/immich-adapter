import asyncio
import pytest
import httpx
from contextvars import copy_context
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import UUID
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routers.middleware.auth_middleware import AuthMiddleware
from routers.utils.gumnut_client import (
    _response_hook,
    get_refreshed_token,
    clear_refreshed_token,
    init_refresh_token_holder,
    set_refreshed_token,
    get_shared_http_client,
)
from services.session_store import Session

# Test session UUID
TEST_SESSION_UUID = UUID("550e8400-e29b-41d4-a716-446655440000")


class TestRefreshTokenHook:
    """Test the response hook that captures refresh tokens."""

    @pytest.mark.anyio
    async def test_capture_refresh_token_hook_with_header(self):
        """Test that the hook captures x-new-access-token header."""
        # Setup
        mock_response = Mock()
        mock_response.headers = {"x-new-access-token": "new-token-123"}

        # Clear any previous token
        clear_refreshed_token()

        # Execute
        await _response_hook(mock_response)

        # Assert
        assert get_refreshed_token() == "new-token-123"

    @pytest.mark.anyio
    async def test_capture_refresh_token_hook_without_header(self):
        """Test that the hook does nothing when header is absent."""
        # Setup
        mock_response = Mock()
        mock_response.headers = {"content-type": "application/json"}

        # Clear and verify
        clear_refreshed_token()
        assert get_refreshed_token() is None

        # Execute
        await _response_hook(mock_response)

        # Assert - should still be None
        assert get_refreshed_token() is None

    @pytest.mark.anyio
    async def test_clear_refreshed_token(self):
        """Test that clear_refreshed_token works correctly."""
        # Setup - set a token
        mock_response = Mock()
        mock_response.headers = {"x-new-access-token": "token-to-clear"}
        await _response_hook(mock_response)
        assert get_refreshed_token() == "token-to-clear"

        # Execute
        clear_refreshed_token()

        # Assert
        assert get_refreshed_token() is None


class TestTokenRefreshIntegration:
    """Integration tests for token refresh through the full stack.

    - Clients send session tokens (UUID)
    - When JWT is refreshed, it's stored in the session (via update_stored_jwt)
    - The session token stays the same, so no cookies/headers need updating
    - The x-new-access-token header is stripped from responses
    """

    @pytest.fixture
    def mock_session_store(self):
        """Create a mock SessionStore."""
        store = AsyncMock()
        now = datetime.now(timezone.utc)
        mock_session = Session(
            id=TEST_SESSION_UUID,
            user_id="user_123",
            library_id="lib_456",
            stored_jwt="encrypted-jwt",
            device_type="iOS",
            device_os="iOS 17",
            app_version="1.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )
        mock_session.get_jwt = MagicMock(return_value="decrypted-jwt-token")
        store.get_by_id.return_value = mock_session
        store.update_stored_jwt.return_value = True
        return store

    @pytest.fixture
    def app_with_mocks(self, mock_session_store):
        """Create a test FastAPI app with mocked dependencies."""
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/api/test/albums")
        async def test_endpoint(request: Request):
            """
            Test endpoint that simulates calling Gumnut backend.

            In real usage, this would call the Gumnut SDK which would
            trigger the response hook if the backend returns a refresh header.
            """
            # Simulate what happens when Gumnut backend returns a refresh header
            mock_response = httpx.Response(
                status_code=200,
                headers={"x-new-access-token": "refreshed-jwt-456"},
                json={"albums": []},
            )
            await _response_hook(mock_response)

            return {"albums": []}

        return app

    @pytest.fixture
    def client_with_mocks(self, app_with_mocks, mock_session_store):
        """Create a test client with mocked session store."""

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "routers.middleware.auth_middleware.get_session_store",
            mock_get_session_store,
        ):
            yield TestClient(app_with_mocks)

    def test_token_refresh_for_web_client(self, client_with_mocks, mock_session_store):
        """Test that JWT refresh updates session store but doesn't change cookies.

        The session token (cookie) stays the same when the JWT is refreshed.
        The refreshed JWT is stored in the session.
        """
        session_token = str(TEST_SESSION_UUID)
        client_with_mocks.cookies.set("immich_access_token", session_token)

        response = client_with_mocks.get("/api/test/albums")

        assert response.status_code == 200
        # Session store should have been called to update the stored JWT
        mock_session_store.update_stored_jwt.assert_called_once_with(
            session_token, "refreshed-jwt-456"
        )
        # Cookie should NOT be updated (session token stays the same)
        set_cookie_header = response.headers.get("set-cookie", "")
        assert "immich_access_token" not in set_cookie_header
        # Refresh header should be stripped
        assert "x-new-access-token" not in response.headers

    def test_token_refresh_for_mobile_client(
        self, client_with_mocks, mock_session_store
    ):
        """Test that JWT refresh updates session store but doesn't send header.

        The session token stays the same when the JWT is refreshed.
        Mobile clients don't need the refresh header since their session token is still valid.
        """
        session_token = str(TEST_SESSION_UUID)
        headers = {"Authorization": f"Bearer {session_token}"}

        response = client_with_mocks.get("/api/test/albums", headers=headers)

        assert response.status_code == 200
        # Session store should have been called to update the stored JWT
        mock_session_store.update_stored_jwt.assert_called_once_with(
            session_token, "refreshed-jwt-456"
        )
        # Refresh header should be stripped (clients don't need it anymore)
        assert "x-new-access-token" not in response.headers

    def test_no_token_refresh_when_backend_doesnt_refresh(self, mock_session_store):
        """Test normal flow when backend doesn't refresh token.

        When the backend doesn't return a refresh header, the session store's
        update_stored_jwt should not be called.
        """
        # Create an app that doesn't trigger token refresh
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/api/test/normal")
        async def normal_endpoint(request: Request):
            # Don't set any refreshed token - simulate no refresh from backend
            return {"data": "ok"}

        session_token = str(TEST_SESSION_UUID)

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "routers.middleware.auth_middleware.get_session_store",
            mock_get_session_store,
        ):
            test_client = TestClient(app)
            test_client.cookies.set("immich_access_token", session_token)

            response = test_client.get("/api/test/normal")

        assert response.status_code == 200
        # update_stored_jwt should NOT be called when there's no refresh
        mock_session_store.update_stored_jwt.assert_not_called()
        # No refresh header should be present
        assert "x-new-access-token" not in response.headers


class TestPerRequestIsolation:
    """The refreshed token must be isolated per request.

    A shared store would let one user's refreshed JWT be persisted into another
    user's session under concurrent load. Each request installs its own holder
    via init_refresh_token_holder(), so concurrent requests cannot observe each
    other's tokens even when running on the same event-loop thread.
    """

    def test_holders_are_isolated_across_contexts(self):
        """Two request contexts each see only their own captured token.

        This documents the baseline isolation property. The cross-contamination
        regression itself is guarded by test_interleaved_requests_do_not_cross_
        contaminate and test_concurrent_response_hooks_stay_isolated below — a
        context that never refreshes must not pick up another's token.
        """

        captured: dict[str, str | None] = {}

        def request_a():
            init_refresh_token_holder()
            set_refreshed_token("jwt-for-A")
            captured["a"] = get_refreshed_token()

        def request_b():
            init_refresh_token_holder()
            set_refreshed_token("jwt-for-B")
            captured["b"] = get_refreshed_token()

        # Run each "request" in its own copied context, the way Starlette runs
        # each request task.
        copy_context().run(request_a)
        copy_context().run(request_b)

        assert captured == {"a": "jwt-for-A", "b": "jwt-for-B"}

    def test_interleaved_requests_do_not_cross_contaminate(self):
        """Even interleaved, a context that never refreshed keeps token=None.

        This is the core regression: previously a request whose own backend call
        did not refresh could still read another concurrent request's refreshed
        token from shared state and persist it into the wrong session.
        """

        ctx_a = copy_context()
        ctx_b = copy_context()

        # Both requests start (install their own holders), interleaved.
        ctx_a.run(init_refresh_token_holder)
        ctx_b.run(init_refresh_token_holder)

        # Only request B's backend call refreshes a token.
        ctx_b.run(set_refreshed_token, "jwt-for-B")

        # Request A, which never refreshed, must not see B's token.
        assert ctx_a.run(get_refreshed_token) is None
        assert ctx_b.run(get_refreshed_token) == "jwt-for-B"

    def test_set_without_init_lazily_installs_holder(self):
        """set_refreshed_token outside the request lifecycle still works.

        A direct call with no init_refresh_token_holder() (e.g. a unit test that
        invokes the hook directly) lazily installs a holder in the current
        context. This stays isolated — the holder lives only in this context.
        """

        def standalone():
            set_refreshed_token("standalone-token")
            return get_refreshed_token()

        assert copy_context().run(standalone) == "standalone-token"

    def test_clear_without_holder_is_noop(self):
        """clear_refreshed_token must not raise when no holder is installed."""

        def standalone():
            clear_refreshed_token()
            return get_refreshed_token()

        assert copy_context().run(standalone) is None

    @pytest.mark.anyio
    async def test_concurrent_response_hooks_stay_isolated(self):
        """Concurrent tasks running the real response hook don't leak tokens."""

        async def handle(token: str, started: asyncio.Event, release: asyncio.Event):
            init_refresh_token_holder()
            response = httpx.Response(
                status_code=200,
                headers={"x-new-access-token": token},
                json={},
            )
            await _response_hook(response)
            started.set()
            # Wait so both tasks are live at the same time before reading back.
            await release.wait()
            return get_refreshed_token()

        release = asyncio.Event()
        started_a = asyncio.Event()
        started_b = asyncio.Event()

        task_a = asyncio.create_task(handle("token-A", started_a, release))
        task_b = asyncio.create_task(handle("token-B", started_b, release))
        await started_a.wait()
        await started_b.wait()
        release.set()

        result_a, result_b = await asyncio.gather(task_a, task_b)
        assert result_a == "token-A"
        assert result_b == "token-B"


class TestTokenRefreshWithMockedGumnut:
    """Test token refresh with mocked Gumnut SDK responses."""

    @pytest.mark.anyio
    async def test_gumnut_response_with_refresh_header(self):
        """Test that httpx response hook captures refresh header from Gumnut."""
        # Clear any previous token
        clear_refreshed_token()

        # Create a mock response that includes refresh header
        mock_response = httpx.Response(
            status_code=200,
            headers={"x-new-access-token": "backend-refreshed-token"},
            json={"albums": []},
        )

        # Get the httpx client (which has our hook registered)
        client = await get_shared_http_client()

        # Simulate the hook being called (as httpx would do with async hooks)
        for hook in client.event_hooks["response"]:
            await hook(mock_response)

        # Assert that the token was captured
        assert get_refreshed_token() == "backend-refreshed-token"

    @pytest.mark.anyio
    async def test_multiple_requests_dont_interfere(self):
        """Sequential hook calls with explicit clears between them.

        Note: production does not rely on manual clearing for isolation — each
        request installs its own holder via init_refresh_token_holder(). See
        TestPerRequestIsolation for the per-request isolation guarantee. This
        test exercises the lower-level set/clear/get sequence within one context.
        """
        client = await get_shared_http_client()

        # First request with refresh
        clear_refreshed_token()
        response1 = httpx.Response(
            status_code=200,
            headers={"x-new-access-token": "token-1"},
            json={},
        )
        for hook in client.event_hooks["response"]:
            await hook(response1)
        assert get_refreshed_token() == "token-1"

        # Second request without refresh (after clearing)
        clear_refreshed_token()
        response2 = httpx.Response(
            status_code=200,
            headers={},
            json={},
        )
        for hook in client.event_hooks["response"]:
            await hook(response2)
        assert get_refreshed_token() is None

        # Third request with different refresh token
        clear_refreshed_token()
        response3 = httpx.Response(
            status_code=200,
            headers={"x-new-access-token": "token-3"},
            json={},
        )
        for hook in client.event_hooks["response"]:
            await hook(response3)
        assert get_refreshed_token() == "token-3"
