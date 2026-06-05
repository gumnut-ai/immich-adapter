"""Attach observability attributes to the active Sentry span.

Emits two things per request:

- `interface` — a low-cardinality tag classifying the Immich client behind
  the request: `immich-mobile-ios`, `immich-mobile-android`, or `immich-web`,
  derived from the User-Agent. Answers "which Immich client is this?" Set as
  both a Sentry tag (so error events group by it) and a span attribute (so the
  `spans` dataset can group `http.server` spans by it). Mirrors photos-api's
  `interface` tag (`mcp` / `rest`) so usage analysis reads one field across
  both services instead of UA-sampling Render logs for the web-vs-mobile
  split. Unrecognized callers (uptime probes, scanners, server-to-server)
  leave it unset, so the aggregation buckets only contain real client traffic.

- `user_agent.original` — the raw `User-Agent` header, following the
  OpenTelemetry semantic convention. Answers "who is the caller?" The Sentry
  SDK doesn't populate this on spans automatically (it only attaches UA to
  error event context / session tracking), so we set it explicitly.
  High-cardinality, so it's a span attribute rather than a tag.

Registered last in `main.py` so it wraps outermost and attaches attributes
even on auth 401 short-circuits from `AuthMiddleware`.
"""

from __future__ import annotations

import re

import sentry_sdk
from fastapi import Request
from sentry_sdk.traces import StreamedSpan
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

INTERFACE_TAG = "interface"
USER_AGENT_ATTRIBUTE = "user_agent.original"

# Immich mobile UAs are `Immich_iOS_<version>` / `Immich_Android_<version>`
# (the format the Immich clients emit). Also accept the lower-case
# `immich-{ios,android}/<version>` form as a safety net for future clients.
_IMMICH_MOBILE_RE = re.compile(
    r"^(?:Immich_(?P<platform1>iOS|Android)_|immich-(?P<platform2>ios|android)/)",
    re.IGNORECASE,
)
# Immich web runs in the browser, so its requests carry a standard browser UA.
_BROWSER_RE = re.compile(r"\b(?:Chrome|Safari|Firefox)\b")


def resolve_interface(user_agent: str) -> str | None:
    """Classify the Immich client behind a request from its User-Agent.

    Returns `immich-mobile-ios`, `immich-mobile-android`, or `immich-web`, or
    `None` when the User-Agent doesn't match a known Immich client (uptime
    probes, scanners, server-to-server) — those spans stay untagged so the
    aggregation buckets only contain real client traffic.
    """
    ua = user_agent or ""

    match = _IMMICH_MOBILE_RE.match(ua)
    if match:
        platform = (match.group("platform1") or match.group("platform2") or "").lower()
        if platform == "android":
            return "immich-mobile-android"
        if platform == "ios":
            return "immich-mobile-ios"

    if ua.startswith("Mozilla/") and _BROWSER_RE.search(ua):
        return "immich-web"

    return None


class ObservabilityTagsMiddleware(BaseHTTPMiddleware):
    """Attach `interface` and `user_agent.original` to the active Sentry span."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        user_agent = request.headers.get("user-agent", "")
        interface = resolve_interface(user_agent)

        # `interface` is low-cardinality — also set it as a scope tag so error
        # events (not just spans) can be grouped by it. UA is high-cardinality,
        # so it stays a span attribute only.
        if interface is not None:
            sentry_sdk.set_tag(INTERFACE_TAG, interface)

        attributes: list[tuple[str, str]] = []
        if interface is not None:
            attributes.append((INTERFACE_TAG, interface))
        if user_agent:
            attributes.append((USER_AGENT_ATTRIBUTE, user_agent))

        span = sentry_sdk.get_current_span()
        if isinstance(span, StreamedSpan):
            for key, value in attributes:
                span.set_attribute(key, value)
        elif span is not None:
            for key, value in attributes:
                span.set_data(key, value)

        return await call_next(request)
