"""Attach observability attributes to the active Sentry span.

Emits two things per request:

- `interface` — a low-cardinality tag classifying the Immich client behind
  the request: `immich-mobile-ios`, `immich-mobile-android`, or `immich-web`.
  Answers "which Immich client is this?" Set as both a Sentry tag (so error
  events group by it) and a span attribute (so the `spans` dataset can group
  `http.server` spans by it). Mirrors photos-api's `interface` tag (`mcp` /
  `rest`) so usage analysis reads one field across both services instead of
  UA-sampling Render logs for the web-vs-mobile split.

  The primary mobile signal is the `deviceType` header the Immich mobile app
  attaches to every API request (`iOS` / `Android`). Native upload/download
  transfers don't send `deviceType` but do set an `immich-ios` / `immich-android`
  User-Agent, so the UA is a fallback for the mobile split. The Immich web SPA
  runs in the browser, so a browser User-Agent classifies as `immich-web`.
  Everything else (uptime probes, scanners, server-to-server) stays unset, so
  the aggregation buckets only hold real, identified client traffic.

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

# The Immich mobile app sets `deviceType: iOS|Android` on every API request
# (immich `mobile/.../api.service.dart::setDeviceInfoHeader`). Other values
# (e.g. `Unknown`) fall through to the UA / browser checks below.
_DEVICE_TYPE_PLATFORMS = {"ios": "ios", "android": "android"}

# Native upload/download transfers carry no `deviceType` but do set an
# `immich-ios/<version>` / `immich-android/<version>` User-Agent (immich
# `mobile/ios/.../URLSessionManager.swift`, `mobile/android/.../HttpClientManager.kt`).
# Also match the legacy `Immich_iOS_<version>` underscore form, which older
# clients emitted and the Immich server still treats as a legacy alias.
_IMMICH_MOBILE_RE = re.compile(
    r"^immich[-_](?P<platform>ios|android)[/_]", re.IGNORECASE
)
# Immich web runs in the browser, so its requests carry a standard browser UA.
_BROWSER_RE = re.compile(r"\b(?:Chrome|Safari|Firefox)\b")


def resolve_interface(device_type: str, user_agent: str) -> str | None:
    """Classify the Immich client behind a request.

    `deviceType` (set by the mobile app on every API call) is the primary
    mobile signal; the `immich-ios` / `immich-android` User-Agent is a fallback
    for native transfers that omit it. A browser User-Agent classifies as web.

    Returns `immich-mobile-ios`, `immich-mobile-android`, or `immich-web`, or
    `None` when neither signal matches a known Immich client (uptime probes,
    scanners, server-to-server) — those spans stay untagged so the aggregation
    buckets only contain real client traffic.
    """
    platform = _DEVICE_TYPE_PLATFORMS.get(device_type.strip().lower())
    if platform is None:
        match = _IMMICH_MOBILE_RE.match(user_agent)
        if match:
            platform = match.group("platform").lower()

    if platform == "ios":
        return "immich-mobile-ios"
    if platform == "android":
        return "immich-mobile-android"

    if user_agent.startswith("Mozilla/") and _BROWSER_RE.search(user_agent):
        return "immich-web"

    return None


class ObservabilityTagsMiddleware(BaseHTTPMiddleware):
    """Attach `interface` and `user_agent.original` to the active Sentry span."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        user_agent = request.headers.get("user-agent", "")
        device_type = request.headers.get("devicetype", "")
        interface = resolve_interface(device_type, user_agent)

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
