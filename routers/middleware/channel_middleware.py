"""Tag Sentry transactions with the interaction channel for immich-adapter.

Every request into immich-adapter gets a `channel` tag indicating how the
user interacted: `immich-mobile-android`, `immich-mobile-ios`, `immich-web`,
or the generic `immich-adapter` fallback when the User-Agent doesn't match
any known client. Tagging at request entry lets Sentry aggregate traffic,
latency, and error rates per channel without sampling Render request logs.
"""

from __future__ import annotations

import re

import sentry_sdk
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

CHANNEL_TAG = "channel"

# Immich mobile app UAs are `Immich_iOS_<version>` / `Immich_Android_<version>`
# (the format emitted by the Immich clients). We also accept the lower-case
# `immich-{ios,android}/<version>` form documented in the GUM-561 spec as a
# safety net for future client versions.
_IMMICH_MOBILE_RE = re.compile(
    r"^(?:Immich_(?P<platform1>iOS|Android)_|immich-(?P<platform2>ios|android)/)",
    re.IGNORECASE,
)

_BROWSER_RE = re.compile(r"\b(Chrome|Safari|Firefox)\b")


def resolve_channel(user_agent: str) -> str:
    """Classify an immich-adapter request into an interaction channel."""
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

    return "immich-adapter"


class ChannelTaggingMiddleware(BaseHTTPMiddleware):
    """Tag the active Sentry transaction with the request's interaction channel."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        channel = resolve_channel(request.headers.get("user-agent", ""))
        sentry_sdk.set_tag(CHANNEL_TAG, channel)
        span = sentry_sdk.get_current_span()
        if span is not None:
            span.set_data(CHANNEL_TAG, channel)

        return await call_next(request)
