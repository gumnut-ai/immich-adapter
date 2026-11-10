"""Targeted redaction for telemetry: signed CDN URLs and session tokens."""

import hashlib
import re
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

_SENSITIVE_CDN_QUERY_KEYS = frozenset({"dl", "verify"})
_REDACTED_QUERY_VALUE = "REDACTED"
_URL_IN_TEXT = re.compile(r"https?://\S+")


def redact_sensitive_cdn_query(query: str) -> str:
    """Redact CDN credentials and filenames without changing other parameters."""
    parts = [part.partition("=") for part in query.split("&")]
    if not any(unquote_plus(key) == "verify" for key, _separator, _value in parts):
        return query

    redacted_parts: list[str] = []
    for key, separator, value in parts:
        if unquote_plus(key) in _SENSITIVE_CDN_QUERY_KEYS:
            redacted_parts.append(f"{key}={_REDACTED_QUERY_VALUE}")
        else:
            redacted_parts.append(f"{key}{separator}{value}")
    return "&".join(redacted_parts)


def redact_sensitive_cdn_url(url: str) -> str:
    """Redact only sensitive CDN query values in a URL-shaped string."""
    try:
        parsed = urlsplit(url)
        redacted_query = redact_sensitive_cdn_query(parsed.query)
        if redacted_query == parsed.query:
            return url
        return urlunsplit(parsed._replace(query=redacted_query))
    except TypeError, ValueError:
        return url


def redact_sensitive_cdn_text(text: str) -> str:
    """Redact signed-CDN URLs or bare queries embedded in telemetry text."""
    redacted = _URL_IN_TEXT.sub(
        lambda match: redact_sensitive_cdn_url(match.group(0)), text
    )
    if redacted != text:
        return redacted
    return redact_sensitive_cdn_query(text)


# A session's bearer token is its UUID, so it is indistinguishable from an
# asset or user id by shape alone. Redact it only where the context marks it as
# a session:
# - text: the Redis keys built from it (`session:<token>`,
#   `session:<token>:checkpoints`), the `/api/sessions/<token>` routes, a
#   `Bearer <token>` credential and the web client's `immich_access_token`
#   cookie;
# - fields: `session_id` / `session_token` in a log record or local variable,
#   and the mobile client's `x-immich-user-token` header, which the SDK's own
#   header filter does not know, whether as a dict key (request data, span
#   attribute, WSGI-style environ) or as an ASGI `[name, value]` header pair.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SESSION_TOKEN_IN_TEXT = re.compile(
    rf"(session:|/sessions/|[Bb]earer\s+|immich_access_token=)({_UUID})"
)
# Frame locals arrive repr-quoted (`'<uuid>'`, `b'<uuid>'`); extras and log
# attributes bare.
_QUOTED = re.compile(r"^b?(['\"])(.*)\1$", re.DOTALL)
_BARE_SESSION_TOKEN = re.compile(rf"^{_UUID}$")
_SESSION_TOKEN_FIELDS = frozenset(
    {
        "session_id",
        "session_token",
        "x-immich-user-token",
        "http_x_immich_user_token",
        "http.request.header.x-immich-user-token",
    }
)


def session_telemetry_id(token: object) -> str:
    """Non-reversible stand-in for a session token in telemetry.

    Stable for a given token, so log records, spans and Socket.IO rooms of one
    session still correlate. A token is a random UUID4, so a truncated SHA-256
    cannot be reversed by enumeration. The result is not UUID-shaped, so the
    scrubbers below leave an already-substituted value unchanged.
    """
    return hashlib.sha256(str(token).encode()).hexdigest()[:16]


def redact_session_tokens_text(text: str) -> str:
    """Replace session tokens that the surrounding text marks as such."""
    return _SESSION_TOKEN_IN_TEXT.sub(
        lambda match: match.group(1) + session_telemetry_id(match.group(2)), text
    )


def _unquote(value: str) -> str:
    match = _QUOTED.match(value)
    return match.group(2) if match else value


def _is_session_token_field(name: object) -> bool:
    return isinstance(name, str) and _unquote(name).lower() in _SESSION_TOKEN_FIELDS


def _redact_field_value(value: str) -> str:
    token = _unquote(value)
    if _BARE_SESSION_TOKEN.match(token):
        return session_telemetry_id(token)
    return redact_session_tokens_text(value)


def redact_session_tokens(value: Any) -> Any:
    """Replace session tokens throughout a JSON-like telemetry payload.

    Mutates dicts and lists in place and returns the (possibly new) value.
    """
    if isinstance(value, str):
        return redact_session_tokens_text(value)
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and _is_session_token_field(key):
                value[key] = _redact_field_value(item)
            else:
                value[key] = redact_session_tokens(item)
    elif isinstance(value, list):
        if (
            len(value) == 2
            and _is_session_token_field(value[0])
            and isinstance(value[1], str)
        ):
            value[1] = _redact_field_value(value[1])
        else:
            for index, item in enumerate(value):
                value[index] = redact_session_tokens(item)
    return value
