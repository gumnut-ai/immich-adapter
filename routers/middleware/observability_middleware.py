"""Attach observability attributes to the active Sentry span.

Currently emits one span attribute:

- `user_agent.original` — the raw `User-Agent` header, following the
  OpenTelemetry semantic convention. Answers "who is the caller?" The
  Sentry SDK doesn't populate this on spans automatically (it only
  attaches UA to error event context / session tracking), so we set it
  explicitly. High-cardinality, so it's a span attribute rather than a
  tag.

Registered last in `main.py` so it wraps outermost and attaches attributes
even on auth 401 short-circuits from `AuthMiddleware`.
"""

from __future__ import annotations

import sentry_sdk
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

USER_AGENT_ATTRIBUTE = "user_agent.original"


class ObservabilityTagsMiddleware(BaseHTTPMiddleware):
    """Attach `user_agent.original` to the active Sentry span."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        user_agent = request.headers.get("user-agent", "")

        if user_agent:
            span = sentry_sdk.get_current_span()
            if span is not None:
                span.set_data(USER_AGENT_ATTRIBUTE, user_agent)

        return await call_next(request)
