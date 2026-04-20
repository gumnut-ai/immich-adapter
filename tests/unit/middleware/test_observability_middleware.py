"""Unit tests for ObservabilityTagsMiddleware."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from routers.middleware.observability_middleware import (
    USER_AGENT_ATTRIBUTE,
    ObservabilityTagsMiddleware,
)


class TestObservabilityTagsMiddleware:
    @pytest.mark.anyio
    async def test_user_agent_is_set_on_span(self):
        """A populated User-Agent header should land on the span as
        `user_agent.original` (OpenTelemetry semantic convention)."""
        app = FastAPI()
        app.add_middleware(ObservabilityTagsMiddleware)

        @app.get("/echo")
        async def _echo():
            return {"ok": True}

        mock_span = MagicMock()
        with patch(
            "routers.middleware.observability_middleware.sentry_sdk.get_current_span",
            return_value=mock_span,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get(
                    "/echo", headers={"user-agent": "Immich_iOS_1.94.0"}
                )

        assert response.status_code == 200
        mock_span.set_data.assert_called_once_with(
            USER_AGENT_ATTRIBUTE, "Immich_iOS_1.94.0"
        )

    @pytest.mark.anyio
    async def test_user_agent_not_set_when_header_missing(self):
        """Missing User-Agent header should not emit an empty
        `user_agent.original` attribute — skip the call entirely so Sentry
        queries can distinguish "attribute absent" from "attribute empty"."""
        app = FastAPI()
        app.add_middleware(ObservabilityTagsMiddleware)

        @app.get("/echo")
        async def _echo():
            return {"ok": True}

        mock_span = MagicMock()
        with patch(
            "routers.middleware.observability_middleware.sentry_sdk.get_current_span",
            return_value=mock_span,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get("/echo", headers={"user-agent": ""})

        assert response.status_code == 200
        mock_span.set_data.assert_not_called()

    @pytest.mark.anyio
    async def test_no_span_does_not_raise(self):
        """No active transaction (span is None) should not raise."""
        app = FastAPI()
        app.add_middleware(ObservabilityTagsMiddleware)

        @app.get("/echo")
        async def _echo():
            return {"ok": True}

        with patch(
            "routers.middleware.observability_middleware.sentry_sdk.get_current_span",
            return_value=None,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get(
                    "/echo", headers={"user-agent": "Immich_Android_1.100.0"}
                )

        assert response.status_code == 200

    @pytest.mark.anyio
    async def test_attribute_set_before_early_rejection(self):
        """Verify ObservabilityTagsMiddleware runs outermost, so responses
        from a downstream middleware that short-circuits (e.g., auth 401)
        still have the UA attribute attached."""

        class _ShortCircuit401(BaseHTTPMiddleware):
            async def dispatch(
                self, request: Request, call_next: RequestResponseEndpoint
            ) -> Response:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)

        app = FastAPI()
        # Mirror main.py registration order: AuthMiddleware added first
        # (innermost), Observability last (outermost, runs first).
        app.add_middleware(_ShortCircuit401)
        app.add_middleware(ObservabilityTagsMiddleware)

        @app.get("/api/protected")
        async def _protected():
            return {"ok": True}

        mock_span = MagicMock()
        with patch(
            "routers.middleware.observability_middleware.sentry_sdk.get_current_span",
            return_value=mock_span,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get(
                    "/api/protected", headers={"user-agent": "Immich_iOS_1.94.0"}
                )

        assert response.status_code == 401
        mock_span.set_data.assert_called_once_with(
            USER_AGENT_ATTRIBUTE, "Immich_iOS_1.94.0"
        )
