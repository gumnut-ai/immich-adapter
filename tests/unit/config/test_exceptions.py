"""
Tests for the global GumnutError exception handler.

These exercise the handler through a minimal FastAPI app + TestClient so
the full request/response/handler pipeline is verified — not just the
handler function in isolation.
"""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gumnut import (
    APIConnectionError,
    APIResponseValidationError,
    AuthenticationError,
    BadRequestError,
    GumnutError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from config.exceptions import configure_exception_handlers
from tests.conftest import make_sdk_status_error


def _make_app(raise_exception: Exception) -> FastAPI:
    """Build a tiny FastAPI app whose `/boom` route raises the given exception."""
    app = FastAPI()
    configure_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise raise_exception

    return app


def _client(exc: Exception) -> TestClient:
    return TestClient(_make_app(exc), raise_server_exceptions=False)


class TestGumnutErrorHandler:
    @pytest.mark.parametrize(
        ("cls", "status_code"),
        [
            (NotFoundError, 404),
            (AuthenticationError, 401),
            (PermissionDeniedError, 403),
            (BadRequestError, 400),
        ],
    )
    def test_typed_status_errors_pass_through_status_code(self, cls, status_code):
        err = make_sdk_status_error(status_code, "upstream said no", cls=cls)
        response = _client(err).get("/boom")

        assert response.status_code == status_code
        body = response.json()
        assert body["statusCode"] == status_code
        assert body["message"]
        assert body["error"]

    def test_extracts_detail_from_body(self):
        err = make_sdk_status_error(
            401,
            "raw",
            body={"detail": "JWT has expired"},
            cls=AuthenticationError,
        )
        response = _client(err).get("/boom")

        assert response.status_code == 401
        assert response.json()["message"] == "JWT has expired"

    def test_internal_server_error_uses_actual_status_code(self):
        # The Stainless-generated InternalServerError carries the real upstream
        # code in .status_code (no Literal override).
        err = make_sdk_status_error(503, "upstream down", cls=InternalServerError)
        response = _client(err).get("/boom")

        assert response.status_code == 503
        assert response.json()["statusCode"] == 503

    def test_rate_limit_error_maps_to_502(self, caplog: pytest.LogCaptureFixture):
        err = make_sdk_status_error(429, "Too many requests", cls=RateLimitError)
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        response = _client(err).get("/boom")

        assert response.status_code == 502
        assert response.json()["message"] == "Upstream temporarily unavailable"

        # Logged at WARNING (under 429 policy), not at the 502 client-facing code.
        rate_limit_records = [
            r for r in caplog.records if "rate-limited request" in r.getMessage()
        ]
        assert rate_limit_records
        assert rate_limit_records[-1].levelno == logging.WARNING

    def test_api_response_validation_error_maps_to_502(self):
        # APIResponseValidationError requires (response, body, message=...)
        import httpx

        request = httpx.Request("GET", "http://test.local/")
        response = httpx.Response(200, request=request)
        err = APIResponseValidationError(response, body=None, message="bad schema")

        client_response = _client(err).get("/boom")
        assert client_response.status_code == 502
        assert client_response.json()["message"] == "Upstream returned invalid response"

    def test_api_connection_error_maps_to_502(self):
        import httpx

        request = httpx.Request("GET", "http://test.local/")
        err = APIConnectionError(request=request)

        response = _client(err).get("/boom")

        assert response.status_code == 502
        assert response.json()["message"] == "Upstream unreachable"

    def test_generic_gumnut_error_maps_to_500(self):
        err = GumnutError("something internal blew up")
        response = _client(err).get("/boom")

        assert response.status_code == 500
        assert response.json()["message"] == "Internal error"

    def test_response_shape_matches_immich_format(self):
        err = make_sdk_status_error(404, "Not found", cls=NotFoundError)
        response = _client(err).get("/boom")

        body = response.json()
        assert set(body.keys()) == {"message", "statusCode", "error"}
        assert body["error"] == "Not Found"

    def test_404_logs_at_info_level(self, caplog: pytest.LogCaptureFixture):
        """Per the upstream policy, 404 is INFO (not WARNING) — these are noisy
        and not actionable."""
        err = make_sdk_status_error(404, "Not found", cls=NotFoundError)
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        _client(err).get("/boom")

        records = [r for r in caplog.records if getattr(r, "status_code", None) == 404]
        assert records
        assert records[-1].levelno == logging.INFO

    def test_5xx_logs_at_error_level(self, caplog: pytest.LogCaptureFixture):
        err = make_sdk_status_error(503, "Down", cls=InternalServerError)
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        _client(err).get("/boom")

        records = [r for r in caplog.records if getattr(r, "status_code", None) == 503]
        assert records
        assert records[-1].levelno == logging.ERROR
