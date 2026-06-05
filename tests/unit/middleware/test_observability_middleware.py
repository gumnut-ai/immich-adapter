"""Unit tests for ObservabilityTagsMiddleware."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from routers.middleware.observability_middleware import (
    INTERFACE_TAG,
    USER_AGENT_ATTRIBUTE,
    ObservabilityTagsMiddleware,
    resolve_interface,
)


class TestResolveInterface:
    """Classification matrix for `resolve_interface(device_type, user_agent)`."""

    @pytest.mark.parametrize(
        ("device_type", "user_agent", "expected"),
        [
            # Primary signal: the deviceType header the mobile app sends on
            # every API request. Covers the bulk of mobile traffic (the Dart
            # OpenAPI client sets no immich User-Agent).
            ("iOS", "", "immich-mobile-ios"),
            ("Android", "", "immich-mobile-android"),
            ("iOS", "Dart/3.3 (dart:io)", "immich-mobile-ios"),
            # deviceType wins over a browser UA (it's the stronger signal; a web
            # request never carries deviceType anyway).
            ("Android", "Mozilla/5.0 (X11) Firefox/121.0", "immich-mobile-android"),
            # Immich's `Unknown` platform isn't a bucket — fall through.
            ("Unknown", "", None),
            # Fallback signal: native upload/download transfers set an
            # immich-ios/android User-Agent but no deviceType. Real clients emit
            # the lower-case `immich-<platform>/<version>` form...
            ("", "immich-ios/1.94.0", "immich-mobile-ios"),
            ("", "immich-android/1.100.0", "immich-mobile-android"),
            # ...and the legacy `Immich_<Platform>_<version>` underscore form is
            # still matched for older clients.
            ("", "Immich_iOS_1.94.0", "immich-mobile-ios"),
            ("", "Immich_Android_1.100.0", "immich-mobile-android"),
            # Immich web runs in a browser — standard browser UAs.
            (
                "",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36",
                "immich-web",
            ),
            (
                "",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Safari/605.1.15",
                "immich-web",
            ),
            (
                "",
                "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) "
                "Gecko/20100101 Firefox/121.0",
                "immich-web",
            ),
            # Unrecognized callers stay unclassified (null bucket): probes,
            # scanners, and a mobile API client that somehow sent no deviceType.
            ("", "curl/8.4.0", None),
            ("", "uptime-kuma/1.23.0", None),
            ("", "Dart/3.3 (dart:io)", None),
            ("", "", None),
            # A bare `Mozilla/` without a known browser token isn't web.
            ("", "Mozilla/5.0 (compatible; SomeBot/1.0)", None),
        ],
    )
    def test_resolve_interface(
        self, device_type: str, user_agent: str, expected: str | None
    ):
        assert resolve_interface(device_type, user_agent) == expected


class TestObservabilityTagsMiddleware:
    @pytest.mark.anyio
    async def test_interface_and_user_agent_set_on_span(self):
        """A mobile API request (deviceType header) lands both the `interface`
        tag and the `interface` / `user_agent.original` span attributes."""
        app = FastAPI()
        app.add_middleware(ObservabilityTagsMiddleware)

        @app.get("/echo")
        async def _echo():
            return {"ok": True}

        mock_span = MagicMock()
        with (
            patch(
                "routers.middleware.observability_middleware.sentry_sdk.get_current_span",
                return_value=mock_span,
            ),
            patch(
                "routers.middleware.observability_middleware.sentry_sdk.set_tag"
            ) as mock_set_tag,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get(
                    "/echo",
                    headers={"deviceType": "iOS", "user-agent": "Dart/3.3 (dart:io)"},
                )

        assert response.status_code == 200
        mock_set_tag.assert_called_once_with(INTERFACE_TAG, "immich-mobile-ios")
        mock_span.set_data.assert_any_call(INTERFACE_TAG, "immich-mobile-ios")
        mock_span.set_data.assert_any_call(USER_AGENT_ATTRIBUTE, "Dart/3.3 (dart:io)")

    @pytest.mark.anyio
    async def test_attributes_set_on_streamed_span(self):
        """Streamed spans receive OpenTelemetry-style attributes via
        `set_attribute` for both `interface` and the UA. Uses a native-transfer
        UA (no deviceType) to exercise the UA fallback path."""
        app = FastAPI()
        app.add_middleware(ObservabilityTagsMiddleware)

        @app.get("/echo")
        async def _echo():
            return {"ok": True}

        class DummyStreamedSpan:
            def __init__(self) -> None:
                self.set_attribute = MagicMock()

        mock_span = DummyStreamedSpan()
        with (
            patch(
                "routers.middleware.observability_middleware.StreamedSpan",
                DummyStreamedSpan,
            ),
            patch(
                "routers.middleware.observability_middleware.sentry_sdk.get_current_span",
                return_value=mock_span,
            ),
            patch("routers.middleware.observability_middleware.sentry_sdk.set_tag"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get(
                    "/echo", headers={"user-agent": "immich-android/1.100.0"}
                )

        assert response.status_code == 200
        mock_span.set_attribute.assert_any_call(INTERFACE_TAG, "immich-mobile-android")
        mock_span.set_attribute.assert_any_call(
            USER_AGENT_ATTRIBUTE, "immich-android/1.100.0"
        )

    @pytest.mark.anyio
    async def test_unrecognized_ua_sets_user_agent_only(self):
        """An unrecognized request emits the `user_agent.original` attribute but
        no `interface` tag or span attribute — the interface bucket stays null."""
        app = FastAPI()
        app.add_middleware(ObservabilityTagsMiddleware)

        @app.get("/echo")
        async def _echo():
            return {"ok": True}

        mock_span = MagicMock()
        with (
            patch(
                "routers.middleware.observability_middleware.sentry_sdk.get_current_span",
                return_value=mock_span,
            ),
            patch(
                "routers.middleware.observability_middleware.sentry_sdk.set_tag"
            ) as mock_set_tag,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get(
                    "/echo", headers={"user-agent": "curl/8.4.0"}
                )

        assert response.status_code == 200
        mock_set_tag.assert_not_called()
        mock_span.set_data.assert_called_once_with(USER_AGENT_ATTRIBUTE, "curl/8.4.0")

    @pytest.mark.anyio
    async def test_nothing_set_when_header_missing(self):
        """A missing User-Agent (and no deviceType) emits neither attribute nor
        tag — skip the calls so Sentry queries can distinguish "absent" from
        "empty"."""
        app = FastAPI()
        app.add_middleware(ObservabilityTagsMiddleware)

        @app.get("/echo")
        async def _echo():
            return {"ok": True}

        mock_span = MagicMock()
        with (
            patch(
                "routers.middleware.observability_middleware.sentry_sdk.get_current_span",
                return_value=mock_span,
            ),
            patch(
                "routers.middleware.observability_middleware.sentry_sdk.set_tag"
            ) as mock_set_tag,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get("/echo", headers={"user-agent": ""})

        assert response.status_code == 200
        mock_set_tag.assert_not_called()
        mock_span.set_data.assert_not_called()

    @pytest.mark.anyio
    async def test_no_span_does_not_raise(self):
        """No active transaction (span is None) should not raise."""
        app = FastAPI()
        app.add_middleware(ObservabilityTagsMiddleware)

        @app.get("/echo")
        async def _echo():
            return {"ok": True}

        with (
            patch(
                "routers.middleware.observability_middleware.sentry_sdk.get_current_span",
                return_value=None,
            ),
            patch("routers.middleware.observability_middleware.sentry_sdk.set_tag"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get("/echo", headers={"deviceType": "Android"})

        assert response.status_code == 200

    @pytest.mark.anyio
    async def test_attributes_set_before_early_rejection(self):
        """Verify ObservabilityTagsMiddleware runs outermost, so responses from
        a downstream middleware that short-circuits (e.g., auth 401) still have
        the `interface` tag and UA attribute attached."""

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
        with (
            patch(
                "routers.middleware.observability_middleware.sentry_sdk.get_current_span",
                return_value=mock_span,
            ),
            patch(
                "routers.middleware.observability_middleware.sentry_sdk.set_tag"
            ) as mock_set_tag,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get(
                    "/api/protected",
                    headers={"deviceType": "iOS", "user-agent": "Dart/3.3 (dart:io)"},
                )

        assert response.status_code == 401
        mock_set_tag.assert_called_once_with(INTERFACE_TAG, "immich-mobile-ios")
        mock_span.set_data.assert_any_call(USER_AGENT_ATTRIBUTE, "Dart/3.3 (dart:io)")
