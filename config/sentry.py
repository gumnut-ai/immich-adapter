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
    """
    for span in event.get("spans", []):
        if span.get("op") != "http.client":
            continue
        data = span.get("data", {})
        if "server.address" in data:
            continue
        description = span.get("description", "")
        parts = description.split(" ", 1)
        if len(parts) != 2:
            continue
        parsed = urlparse(parts[1])
        if parsed.hostname:
            span.setdefault("data", {})["server.address"] = parsed.hostname
            if parsed.port:
                span["data"]["server.port"] = parsed.port
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
