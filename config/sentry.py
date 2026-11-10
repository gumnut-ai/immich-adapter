import contextlib
import logging
import os
from typing import Any
from urllib.parse import urlparse

import sentry_sdk
from sentry_sdk.types import Event, Hint
from sentry_sdk.utils import iter_event_frames

from config.settings import get_settings
from config.telemetry import (
    redact_sensitive_cdn_query,
    redact_sensitive_cdn_url,
    redact_sensitive_cdn_values,
    redact_session_tokens,
)

logger = logging.getLogger(__name__)

# Stack locals can hold a session bearer token under arbitrary names:
# `session_token`, `session_uuid`, a session route's `id` path parameter, the
# ASGI `scope` and Socket.IO `environ` (request headers and path parameters),
# redis-py's command arguments. `include_local_variables` stays at its default
# (True) and the SDK scrubber matches only exact well-known names, so locals are
# dropped wholesale from every library and standard-library frame (`in_app` not
# true), and from the adapter's own frames in the modules below. A new adapter
# module that handles session tokens belongs here.
_SESSION_TOKEN_MODULES = (
    "routers.api.auth",
    "routers.api.oauth",
    "routers.api.sessions",
    "routers.api.sync.routes",
    "routers.middleware",
    "routers.utils.cookies",
    "routers.utils.gumnut_client",
    "routers.utils.thumbnail_cache",
    "services.checkpoint_store",
    "services.library_resolver",
    "services.session_store",
    "services.websockets",
)


def _in_session_token_module(module: object) -> bool:
    return isinstance(module, str) and any(
        module == name or module.startswith(name + ".")
        for name in _SESSION_TOKEN_MODULES
    )


def _keeps_frame_vars(frame: dict[str, Any]) -> bool:
    return frame.get("in_app") is True and not _in_session_token_module(
        frame.get("module")
    )


def _scrub_session_tokens(event: Any) -> Any:
    """Drop stack locals that may hold a session token, then substitute any
    token left in the payload with its telemetry id (see `config.telemetry`)."""
    for frame in iter_event_frames(event):
        if isinstance(frame, dict) and "vars" in frame and not _keeps_frame_vars(frame):
            frame["vars"] = {"scrubbed": "[Filtered]"}
    return redact_session_tokens(event)


def _redact_error_event(event: Event, _hint: Hint) -> Event | None:
    """Remove signed-CDN credentials, filenames and session tokens from error
    event values."""
    # before_send must never drop an otherwise useful error event over CDN
    # redaction.
    with contextlib.suppress(Exception):
        event = redact_sensitive_cdn_values(event)
    try:
        return _scrub_session_tokens(event)
    except Exception:
        # A session token is a live credential: drop the event rather than
        # risk sending one.
        return None


def _before_send_transaction(event: Event, hint: Hint) -> Event | None:
    """Scrub session tokens from spans and request data, then enrich spans."""
    try:
        event = _scrub_session_tokens(event)
    except Exception:
        return None
    return _enrich_http_spans(event, hint)


def _before_send_log(log: Any, _hint: Hint) -> Any:
    """Scrub signed-CDN credentials, filenames and session tokens from a log's
    body and attributes. A message that formats an `httpx.HTTPStatusError`
    quotes the full signed request URL."""
    try:
        return redact_session_tokens(redact_sensitive_cdn_values(log))
    except Exception:
        return None


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


def sentry_init_options(dsn: str) -> dict[str, Any]:
    """Keyword arguments for ``sentry_sdk.init``, shared with telemetry tests."""
    return {
        "dsn": dsn,
        "release": os.environ.get("RENDER_GIT_COMMIT"),
        "_experiments": {
            "enable_logs": True,
        },
        "traces_sample_rate": 0.1,
        "profiles_sample_rate": 0.1,
        # Profiles will be automatically collected while
        # there is an active span.
        "profile_lifecycle": "trace",
        "environment": get_settings().environment,
        "before_send": _redact_error_event,
        "before_send_transaction": _before_send_transaction,
        "before_send_log": _before_send_log,
    }


def init_sentry():
    """Initialize Sentry for logging, error tracking, and monitoring."""
    sentry_dsn = get_settings().sentry_dsn
    if sentry_dsn:
        sentry_sdk.init(**sentry_init_options(sentry_dsn))
    else:
        logger.info("Sentry disabled: SENTRY_DSN is empty or not set")
