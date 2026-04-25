"""Tests for the Gumnut SDK exception handler middleware."""

import json
import logging

import httpx
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


def _response(status_code: int, body: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://upstream.example/v1/things")
    return httpx.Response(
        status_code=status_code,
        request=request,
        content=json.dumps(body or {}).encode("utf-8"),
    )


@pytest.fixture
def app() -> FastAPI:
    """A FastAPI app with the exception handlers wired up and routes that raise."""
    app = FastAPI()
    configure_exception_handlers(app)

    @app.get("/raise")
    async def _raise(exc_type: str):
        request = httpx.Request("GET", "https://upstream.example/v1/things")
        if exc_type == "rate_limit":
            raise RateLimitError(
                message="rate limited",
                response=_response(429),
                body=None,
            )
        if exc_type == "not_found":
            raise NotFoundError(
                message="missing",
                response=_response(404),
                body={"detail": "missing"},
            )
        if exc_type == "auth":
            raise AuthenticationError(
                message="auth failed",
                response=_response(401),
                body={"message": "JWT expired"},
            )
        if exc_type == "permission":
            raise PermissionDeniedError(
                message="forbidden",
                response=_response(403),
                body={"error": "forbidden"},
            )
        if exc_type == "bad_request":
            raise BadRequestError(
                message="bad",
                response=_response(400),
                body={"detail": "validation failed"},
            )
        if exc_type == "internal":
            raise InternalServerError(
                message="boom",
                response=_response(503),
                body=None,
            )
        if exc_type == "connection":
            raise APIConnectionError(request=request)
        if exc_type == "response_validation":
            raise APIResponseValidationError(
                response=_response(200),
                body={"unexpected": "shape"},
            )
        if exc_type == "generic":
            raise GumnutError("something broke")
        if exc_type == "non_dict_body":
            raise NotFoundError(
                message="missing",
                response=_response(404),
                body="raw text",
            )
        raise ValueError(f"unknown exc_type {exc_type!r}")

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class TestRateLimitErrorMapping:
    def test_returns_502_not_429(self, client: TestClient):
        response = client.get("/raise", params={"exc_type": "rate_limit"})
        assert response.status_code == 502
        body = response.json()
        assert body["statusCode"] == 502
        assert "Upstream temporarily unavailable" in body["message"]
        assert body["error"] == "Bad Gateway"

    def test_logs_at_warning(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ):
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")
        client.get("/raise", params={"exc_type": "rate_limit"})
        records = [
            r
            for r in caplog.records
            if r.getMessage().startswith("SDK retries exhausted")
        ]
        assert records
        assert records[-1].levelno == logging.WARNING


class TestAPIStatusErrorMapping:
    def test_404_passes_through_with_body_detail(self, client: TestClient):
        response = client.get("/raise", params={"exc_type": "not_found"})
        assert response.status_code == 404
        body = response.json()
        assert body["statusCode"] == 404
        assert body["message"] == "missing"
        assert body["error"] == "Not Found"

    def test_401_uses_body_message(self, client: TestClient):
        response = client.get("/raise", params={"exc_type": "auth"})
        assert response.status_code == 401
        assert response.json()["message"] == "JWT expired"

    def test_403_uses_body_error(self, client: TestClient):
        response = client.get("/raise", params={"exc_type": "permission"})
        assert response.status_code == 403
        assert response.json()["message"] == "forbidden"

    def test_400_uses_body_detail(self, client: TestClient):
        response = client.get("/raise", params={"exc_type": "bad_request"})
        assert response.status_code == 400
        assert response.json()["message"] == "validation failed"

    def test_5xx_passes_through(self, client: TestClient):
        response = client.get("/raise", params={"exc_type": "internal"})
        assert response.status_code == 503

    def test_404_logs_at_info(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ):
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")
        client.get("/raise", params={"exc_type": "not_found"})
        records = [r for r in caplog.records if getattr(r, "status_code", None) == 404]
        assert records
        assert records[-1].levelno == logging.INFO

    def test_5xx_logs_at_error(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ):
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")
        client.get("/raise", params={"exc_type": "internal"})
        records = [r for r in caplog.records if getattr(r, "status_code", None) == 503]
        assert records
        assert records[-1].levelno == logging.ERROR

    def test_non_dict_body_falls_back_to_message(self, client: TestClient):
        response = client.get("/raise", params={"exc_type": "non_dict_body"})
        assert response.status_code == 404
        assert response.json()["message"] == "missing"


class TestAPIConnectionErrorMapping:
    def test_returns_502(self, client: TestClient):
        response = client.get("/raise", params={"exc_type": "connection"})
        assert response.status_code == 502
        assert response.json()["message"] == "Upstream unreachable"

    def test_logs_at_error(self, client: TestClient, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")
        client.get("/raise", params={"exc_type": "connection"})
        records = [r for r in caplog.records if "connection error" in r.getMessage()]
        assert records
        assert records[-1].levelno == logging.ERROR


class TestAPIResponseValidationErrorMapping:
    def test_returns_502(self, client: TestClient):
        response = client.get("/raise", params={"exc_type": "response_validation"})
        assert response.status_code == 502
        assert response.json()["message"] == "Upstream returned invalid response"


class TestGenericGumnutErrorMapping:
    def test_returns_500(self, client: TestClient):
        response = client.get("/raise", params={"exc_type": "generic"})
        assert response.status_code == 500
        assert response.json()["message"] == "Internal error"


class TestImmichResponseShape:
    def test_response_includes_message_status_code_and_error_phrase(
        self, client: TestClient
    ):
        response = client.get("/raise", params={"exc_type": "not_found"})
        body = response.json()
        assert set(body.keys()) == {"message", "statusCode", "error"}
        assert isinstance(body["message"], str)
        assert isinstance(body["statusCode"], int)
        assert isinstance(body["error"], str)
