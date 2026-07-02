"""Unit tests for Auth API functions."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from socketio.exceptions import SocketIOError

from config.exceptions import configure_exception_handlers
from routers.api.auth import post_logout, router as auth_router, validate_access_token
from routers.middleware.auth_middleware import AuthMiddleware
from routers.utils.cookies import ImmichCookie
from services.session_store import Session, SessionStore
from services.websockets import WebSocketEvent


class TestPostLogout:
    """Tests for logout session deletion."""

    @pytest.fixture
    def mock_session_store(self):
        """Create a mock SessionStore."""
        store = AsyncMock(spec=SessionStore)
        return store

    @pytest.fixture
    def mock_request(self):
        """Create a mock request."""
        request = Mock()
        request.cookies = {}
        request.state = Mock(spec=[])  # No session_token attribute by default
        return request

    @pytest.fixture
    def mock_response(self):
        """Create a mock response."""
        return Mock()

    @pytest.mark.anyio
    async def test_deletes_session_from_request_state(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that session is deleted when session token is in request.state."""
        mock_request.state.session_token = "test-session-token"
        mock_request.cookies = {}

        with patch("routers.api.auth.emit_session_event", new_callable=AsyncMock):
            result = await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

        # Verify session was deleted
        mock_session_store.delete.assert_called_once_with("test-session-token")
        assert result.successful is True

    @pytest.mark.anyio
    async def test_deletes_session_from_cookie_when_not_in_state(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that session is deleted when session token is in cookie but not in state."""
        mock_request.state = Mock(spec=[])  # No session_token attribute
        mock_request.cookies = {
            ImmichCookie.ACCESS_TOKEN.value: "cookie-session-token",
        }

        with patch("routers.api.auth.emit_session_event", new_callable=AsyncMock):
            result = await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

        # Verify session was deleted using cookie value
        mock_session_store.delete.assert_called_once_with("cookie-session-token")
        assert result.successful is True

    @pytest.mark.anyio
    async def test_logout_succeeds_when_no_jwt_present(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout succeeds even when no session token is present."""
        mock_request.state = Mock(spec=[])  # No session_token attribute
        mock_request.cookies = {}

        with patch("routers.api.auth.emit_session_event", new_callable=AsyncMock):
            result = await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

        # Session delete should not be called
        mock_session_store.delete.assert_not_called()
        # But logout should still succeed
        assert result.successful is True

    @pytest.mark.anyio
    async def test_logout_succeeds_when_session_deletion_fails(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout succeeds even if session deletion fails."""
        mock_request.state.session_token = "test-session-token"
        mock_request.cookies = {}

        # Session deletion will fail
        mock_session_store.delete.side_effect = Exception("Redis connection failed")

        result = await post_logout(
            request=mock_request,
            response=mock_response,
            client=None,
            session_store=mock_session_store,
        )

        # Logout should still succeed
        assert result.successful is True
        # Cookies should still be deleted
        mock_response.delete_cookie.assert_any_call(ImmichCookie.ACCESS_TOKEN.value)
        mock_response.delete_cookie.assert_any_call(ImmichCookie.AUTH_TYPE.value)
        mock_response.delete_cookie.assert_any_call(ImmichCookie.IS_AUTHENTICATED.value)

    @pytest.mark.anyio
    async def test_logout_deletes_cookies(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout always deletes auth cookies."""
        mock_request.state.session_token = "test-session-token"
        mock_request.cookies = {}

        with patch("routers.api.auth.emit_session_event", new_callable=AsyncMock):
            await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

        # All auth cookies should be deleted
        mock_response.delete_cookie.assert_any_call(ImmichCookie.ACCESS_TOKEN.value)
        mock_response.delete_cookie.assert_any_call(ImmichCookie.AUTH_TYPE.value)
        mock_response.delete_cookie.assert_any_call(ImmichCookie.IS_AUTHENTICATED.value)

    @pytest.mark.anyio
    async def test_logout_returns_correct_redirect_uri(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout returns the correct redirect URI for non-OAuth logout."""
        mock_request.state = Mock(spec=[])
        mock_request.cookies = {}

        with patch("routers.api.auth.emit_session_event", new_callable=AsyncMock):
            result = await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

        assert result.redirectUri == "/auth/login?autoLaunch=0"
        assert result.successful is True

    @pytest.mark.anyio
    async def test_logout_emits_websocket_event(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that logout emits on_session_delete WebSocket event."""
        mock_request.state.session_token = "test-session-token"
        mock_request.cookies = {}

        with patch(
            "routers.api.auth.emit_session_event", new_callable=AsyncMock
        ) as mock_emit:
            await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

            mock_emit.assert_called_once()
            call = mock_emit.call_args
            assert call[0][0] == WebSocketEvent.SESSION_DELETE
            assert call[0][1] == "test-session-token"
            assert call[0][2] == "test-session-token"

    @pytest.mark.anyio
    async def test_logout_websocket_error_does_not_fail_logout(
        self, mock_request, mock_response, mock_session_store
    ):
        """Test that WebSocket emission errors don't fail logout."""
        mock_request.state.session_token = "test-session-token"
        mock_request.cookies = {}

        # Patch the underlying emit so the SocketIOError originates *inside*
        # emit_session_event (which now swallows it centrally).
        with patch(
            "services.websockets._emit_event",
            new_callable=AsyncMock,
            side_effect=SocketIOError("WebSocket error"),
        ):
            result = await post_logout(
                request=mock_request,
                response=mock_response,
                client=None,
                session_store=mock_session_store,
            )

            # Logout should still succeed despite WebSocket error
            assert result.successful is True
            mock_session_store.delete.assert_called_once()


class TestValidateAccessToken:
    """Unit tests for the /api/auth/validateToken handler.

    The Immich auth guard calls this on navigation and trusts the result, so a
    present-but-*expired* JWT must fail here, not just a missing one. The handler
    probes the backend with users.me(); an expired token raises
    AuthenticationError (mapped to a 401 by the global handler), which bounces
    the client back to login.
    """

    @pytest.mark.anyio
    async def test_returns_auth_status_true_when_backend_accepts_jwt(self):
        """authStatus=True when the backend accepts the JWT (users.me succeeds)."""
        gumnut_client = Mock()
        gumnut_client.users.me = AsyncMock(return_value=Mock())

        result = await validate_access_token(gumnut_client)

        assert result.authStatus is True
        gumnut_client.users.me.assert_awaited_once()

    @pytest.mark.anyio
    async def test_propagates_auth_error_when_jwt_expired(self):
        """An expired JWT makes users.me() raise AuthenticationError, which must
        propagate so the global GumnutError handler maps it to a 401."""
        import httpx
        from gumnut import AuthenticationError

        req = httpx.Request("GET", "http://test/api/users/me")
        resp = httpx.Response(401, json={"detail": "JWT has expired"}, request=req)
        gumnut_client = Mock()
        gumnut_client.users.me = AsyncMock(
            side_effect=AuthenticationError(
                "Error code: 401",
                response=resp,
                body={"detail": "JWT has expired"},
            )
        )

        with pytest.raises(AuthenticationError):
            await validate_access_token(gumnut_client)


class TestValidateAccessTokenIntegration:
    """Integration tests for /api/auth/validateToken through the auth middleware.

    The unit tests above pass the client in directly, so they do not verify that
    the middleware populates jwt_token and the dependency forwards it. These
    exercise the middleware → dependency → endpoint wiring end-to-end, patching
    only the SDK client construction so no real backend call is made:

    - an authenticated request reaches the backend probe and returns authStatus=True,
    - an unauthenticated request 401s in the dependency before any probe, and
    - a request whose JWT the backend rejects 401s in Immich's error shape (the
      iOS auth-guard bug this fix addresses).
    """

    TEST_SESSION_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
    TEST_JWT = "test.jwt.token"

    @pytest.fixture
    def mock_session_store(self):
        """SessionStore that returns a session whose decrypted JWT is TEST_JWT."""
        store = AsyncMock(spec=SessionStore)
        now = datetime.now(timezone.utc)
        session = Session(
            id=self.TEST_SESSION_ID,
            user_id="user_123",
            library_id="lib_456",
            stored_jwt="encrypted-placeholder",
            device_type="iOS",
            device_os="iOS 17.4",
            app_version="1.94.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )
        session.get_jwt = MagicMock(return_value=self.TEST_JWT)
        store.get_by_id.return_value = session
        return store

    @pytest.fixture
    def client(self, mock_session_store):
        """TestClient for an app wired with the real auth router + middleware."""
        app = FastAPI()
        app.add_middleware(AuthMiddleware)
        app.include_router(auth_router)
        configure_exception_handlers(app)

        async def mock_get_session_store():
            return mock_session_store

        with patch(
            "routers.middleware.auth_middleware.get_session_store",
            mock_get_session_store,
        ):
            yield TestClient(app)

    def test_authenticated_request_returns_auth_status_true(self, client):
        """Authed bearer + backend accepts JWT → 200 + authStatus=True.

        Patches get_gumnut_client (not the dependency) so the real dependency
        still reads the middleware-populated jwt_token and forwards it.
        """
        mock_client = Mock()
        mock_client.users.me = AsyncMock(return_value=Mock())
        headers = {"Authorization": f"Bearer {self.TEST_SESSION_ID}"}

        with patch(
            "routers.utils.gumnut_client.get_gumnut_client",
            AsyncMock(return_value=mock_client),
        ) as mock_get_client:
            response = client.post("/api/auth/validateToken", headers=headers)

        assert response.status_code == 200
        assert response.json() == {"authStatus": True}
        # The middleware-populated JWT was forwarded to the SDK client.
        mock_get_client.assert_awaited_once_with(self.TEST_JWT)

    def test_unauthenticated_request_returns_401(self, client):
        """No auth → 401 in Immich's error shape, raised by the dependency before
        any backend probe."""
        response = client.post("/api/auth/validateToken")

        assert response.status_code == 401
        body = response.json()
        assert body["statusCode"] == 401
        assert body["message"] == "Authentication required"

    def test_expired_jwt_returns_401(self, client):
        """Authed session but the backend rejects the JWT → 401 "JWT has expired".

        This is the fix: a present-but-expired JWT must fail validateToken so the
        iOS auth guard bounces the user to login instead of trusting a stale token.
        """
        import httpx
        from gumnut import AuthenticationError

        req = httpx.Request("GET", "http://test/api/users/me")
        resp = httpx.Response(401, json={"detail": "JWT has expired"}, request=req)
        mock_client = Mock()
        mock_client.users.me = AsyncMock(
            side_effect=AuthenticationError(
                "Error code: 401",
                response=resp,
                body={"detail": "JWT has expired"},
            )
        )
        headers = {"Authorization": f"Bearer {self.TEST_SESSION_ID}"}

        with patch(
            "routers.utils.gumnut_client.get_gumnut_client",
            AsyncMock(return_value=mock_client),
        ):
            response = client.post("/api/auth/validateToken", headers=headers)

        assert response.status_code == 401
        body = response.json()
        assert body["statusCode"] == 401
        assert "JWT has expired" in body["message"]
