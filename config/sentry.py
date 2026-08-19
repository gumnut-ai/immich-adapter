import logging
import os
from typing import Any
from urllib.parse import urlparse

import sentry_sdk
from sentry_sdk.types import Event, Hint

from config.settings import get_settings
from config.telemetry import (
    redact_sensitive_cdn_query,
    redact_sensitive_cdn_text,
    redact_sensitive_cdn_url,
)

logger = logging.getLogger(__name__)


def _redact_error_event(event: Event, _hint: Hint) -> Event:
    """Remove signed-CDN credentials and filenames from error event values."""

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            return redact_sensitive_cdn_text(value)
        if isinstance(value, dict):
            for key, item in value.items():
                value[key] = redact(item)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = redact(item)
        return value

    try:
        redact(event)
        return event
    except Exception:
        # before_send must never drop an otherwise useful error event.
        return event


def _enrich_http_spans(event, _hint):
    """Add server.address to http.client spans for the Sentry Requests module.

    The sentry-sdk httpx integration (as of v2.48.0) does not set
    server.address on http.client spans. Without this attribute, spans
    are invisible on the Sentry Requests dashboard.

    This hook must be strictly non-throwing — any exception drops the
    entire transaction event.
    """
    for span in event.get("spans") or []:
        if not isinstance(span, dict):
            continue
        if span.get("op") != "http.client":
            continue
        data = span.get("data")
        if not isinstance(data, dict):
            data = {}

        query = data.get("http.query")
        if isinstance(query, str):
            data["http.query"] = redact_sensitive_cdn_query(query)

        data_url = data.get("url")
        if isinstance(data_url, str):
            data["url"] = redact_sensitive_cdn_url(data_url)

        description_url: str | None = None
        description = span.get("description")
        if isinstance(description, str):
            parts = description.split(" ", 1)
            if len(parts) == 2:
                description_url = redact_sensitive_cdn_url(parts[1])
                span["description"] = f"{parts[0]} {description_url}"

        if "server.address" in data:
            span["data"] = data
            continue

        url = data.get("url")
        if not isinstance(url, str) or not url:
            if description_url is None:
                continue
            url = description_url

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue
        if not parsed.hostname:
            continue

        data["server.address"] = parsed.hostname
        try:
            if parsed.port is not None:
                data["server.port"] = parsed.port
        except ValueError:
            pass
        span["data"] = data
    return event


def init_sentry():
    """Initialize Sentry for logging, error tracking, and monitoring."""
    sentry_dsn = get_settings().sentry_dsn
    if sentry_dsn:
        sentry_sdk.init(
            dsn=sentry_dsn,
            release=os.environ.get("RENDER_GIT_COMMIT"),
            _experiments={
                "enable_logs": True,
            },
            traces_sample_rate=0.1,
            profiles_sample_rate=0.1,
            # Profiles will be automatically collected while
            # there is an active span.
            profile_lifecycle="trace",
            environment=get_settings().environment,
            before_send=_redact_error_event,
            before_send_transaction=_enrich_http_spans,
        )
    else:
        logger.info("Sentry disabled: SENTRY_DSN is empty or not set")
