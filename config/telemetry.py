"""Targeted redaction for telemetry: signed CDN URLs and session tokens."""

import hashlib
import re
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit
from uuid import UUID

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


def redact_sensitive_cdn_values(value: Any) -> Any:
    """Return a JSON-like telemetry payload with signed-CDN credentials and
    filenames redacted in every string.

    Builds new containers rather than mutating: log attributes reference the
    caller's own `extra` objects.
    """
    if isinstance(value, str):
        return redact_sensitive_cdn_text(value)
    if isinstance(value, dict):
        return {key: redact_sensitive_cdn_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_cdn_values(item) for item in value]
    return value


# A session's bearer token is its UUID, so it is indistinguishable from an
# asset or user id by shape alone. Redact it only where the context marks it as
# a session:
# - text: the Redis keys built from it (`session:<token>`,
#   `session:<token>:checkpoints`), the `/api/sessions/<id>` routes (which take
#   a public session id, but a page that listed sessions by token before that
#   may still send one), a `Bearer <token>` credential, the web client's `immich_access_token`
#   cookie, and the session index commands that redis-py quotes in pipeline
#   errors (`SREM user:<id>:sessions <token>`,
#   `ZADD sessions:by_updated_at <score> <token>`);
# - fields: `session_id` / `session_token` in a log record or local variable,
#   and the mobile client's `x-immich-user-token` header, which the SDK's own
#   header filter does not know, whether as a dict key (request data,
#   WSGI-style environ) or as an ASGI `[name, value]` header pair. A field's
#   value may be a string, a `UUID` or a list of either.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SESSION_TOKEN_IN_TEXT = re.compile(
    r"(session:|/sessions/|[Bb]earer\s+|immich_access_token=|:sessions\s+"
    r"|sessions:by_updated_at\s+(?:[0-9.eE+-]+\s+)?)"
    rf"({_UUID})"
)
# Serialized frame locals arrive as reprs (`'<uuid>'`, `b'<uuid>'`,
# `UUID('<uuid>')`); extras and log attributes as the values themselves.
_QUOTED = re.compile(r"^(?:b|UUID\()?(['\"])(.*)\1\)?$", re.DOTALL)
_BARE_SESSION_TOKEN = re.compile(rf"^{_UUID}$")
_SESSION_TOKEN_FIELDS = frozenset(
    {
        "session_id",
        "session_token",
        "x-immich-user-token",
        "http_x_immich_user_token",
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


def _redact_field_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return session_telemetry_id(value)
    if isinstance(value, str):
        token = _unquote(value)
        if _BARE_SESSION_TOKEN.match(token):
            return session_telemetry_id(token)
        return redact_session_tokens_text(value)
    if isinstance(value, (list, tuple)):
        return [_redact_field_value(item) for item in value]
    return redact_session_tokens(value)


def redact_session_tokens(value: Any) -> Any:
    """Return a JSON-like telemetry payload with session tokens replaced.

    Builds new containers rather than mutating: log attributes reference the
    caller's own `extra` objects.
    """
    if isinstance(value, str):
        return redact_session_tokens_text(value)
    if isinstance(value, dict):
        return {
            key: _redact_field_value(item)
            if _is_session_token_field(key)
            else redact_session_tokens(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and _is_session_token_field(value[0]):
            return [value[0], _redact_field_value(value[1])]
        return [redact_session_tokens(item) for item in value]
    return value
