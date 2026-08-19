"""Tests for targeted HTTP client log redaction."""

import io
import logging

import httpx
import pytest

from config.logging import LOGGING_CONFIG, RedactSensitiveHttpxQuery


@pytest.mark.anyio
async def test_httpx_logs_redact_only_signed_cdn_values() -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactSensitiveHttpxQuery())
    httpx_logger = logging.getLogger("httpx")
    prior_handlers = httpx_logger.handlers[:]
    prior_level = httpx_logger.level
    prior_propagate = httpx_logger.propagate
    httpx_logger.handlers = [handler]
    httpx_logger.setLevel(logging.INFO)
    httpx_logger.propagate = False

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await client.get(
                "https://assets.gumnut.ai/asset.jpg"
                "?w=720&verify=secret-token&dl=family-photo.jpg&f=webp"
            )
            await client.get(
                "https://api.example.com/search?page=2&dl=report.csv&query=cats"
            )
    finally:
        httpx_logger.handlers = prior_handlers
        httpx_logger.setLevel(prior_level)
        httpx_logger.propagate = prior_propagate

    logs = output.getvalue()
    assert "secret-token" not in logs
    assert "family-photo.jpg" not in logs
    assert "w=720&verify=REDACTED&dl=REDACTED&f=webp" in logs
    assert "search?page=2&dl=report.csv&query=cats" in logs


def test_default_handler_installs_httpx_redaction_filter() -> None:
    assert LOGGING_CONFIG["handlers"]["default"]["filters"] == [
        "redact_sensitive_httpx_query"
    ]
