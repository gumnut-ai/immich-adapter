import logging
import os
from urllib.parse import urlparse

import sentry_sdk

from config.settings import get_settings

logger = logging.getLogger(__name__)


def _enrich_http_spans(event, _hint):
    """Add server.address to http.client spans for the Sentry Requests module.

    The sentry-sdk httpx integration (as of v2.48.0) does not set
    server.address on http.client spans. Without this attribute, spans
    are invisible on the Sentry Requests dashboard.

    This hook must be strictly non-throwing — any exception drops the
    entire transaction event.
    """
    for span in event.get("spans") or []:
        if span.get("op") != "http.client":
            continue
        data = span.get("data")
        if not isinstance(data, dict):
            data = {}
        if "server.address" in data:
            continue

        url = data.get("url")
        if not url:
            parts = (span.get("description") or "").split(" ", 1)
            if len(parts) < 2:
                continue
            url = parts[1]

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
            before_send_transaction=_enrich_http_spans,
        )
    else:
        logger.info("Sentry disabled: SENTRY_DSN is empty or not set")
