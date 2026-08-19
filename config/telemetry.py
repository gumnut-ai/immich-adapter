"""Targeted redaction for signed CDN telemetry."""

import re
from urllib.parse import unquote_plus, urlsplit, urlunsplit

_SENSITIVE_CDN_QUERY_KEYS = frozenset({"dl", "verify"})
_REDACTED_QUERY_VALUE = "REDACTED"
_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>\[\]{}(),]+")


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
    except (TypeError, ValueError):
        return url


def redact_sensitive_cdn_text(text: str) -> str:
    """Redact signed-CDN URLs or bare queries embedded in telemetry text."""
    redacted = _URL_IN_TEXT.sub(
        lambda match: redact_sensitive_cdn_url(match.group(0)), text
    )
    if redacted != text:
        return redacted
    return redact_sensitive_cdn_query(text)
