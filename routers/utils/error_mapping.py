"""
Error mapping utilities for handling Gumnut SDK exceptions.
"""

import logging
from typing import Any

from fastapi import HTTPException, status
from gumnut import RateLimitError

logger = logging.getLogger(__name__)


def upstream_status_log_level(status_code: int) -> int:
    """Return log level for upstream HTTP responses.

    Policy:
    - 404 -> INFO
    - Other 4xx -> WARNING
    - 5xx -> ERROR
    - Everything else -> INFO
    """
    if status_code == status.HTTP_404_NOT_FOUND:
        return logging.INFO
    if 400 <= status_code < 500:
        return logging.WARNING
    if status_code >= 500:
        return logging.ERROR
    return logging.INFO


def log_upstream_response(
    logger_obj: logging.Logger,
    *,
    context: str,
    status_code: int,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log an upstream response/error using the shared status-to-level policy."""
    log_extra: dict[str, Any] = {
        "context": context,
        "status_code": status_code,
    }
    if extra:
        log_extra.update(extra)

    logger_obj.log(
        upstream_status_log_level(status_code),
        message,
        extra=log_extra,
    )


def get_upstream_status_code(e: Exception) -> int | None:
    """Best-effort extraction of upstream HTTP status code from an exception."""
    if hasattr(e, "status_code"):
        try:
            return int(getattr(e, "status_code"))
        except TypeError, ValueError:
            return None

    msg = str(e)
    msg_lower = msg.lower()
    if "404" in msg or "not found" in msg_lower:
        return 404
    if "401" in msg or "invalid api key" in msg_lower or "unauthorized" in msg_lower:
        return 401
    if "403" in msg or "forbidden" in msg_lower:
        return 403
    if "400" in msg or "bad request" in msg_lower:
        return 400

    return None


def check_for_error_by_code(e: Exception, code: int) -> bool:
    """
    Check if an exception represents a specific HTTP error code.

    Args:
        e: The exception to check
        code: The HTTP status code to check for

    Returns:
        True if the exception represents the specified error code, False otherwise
    """
    # Check if the SDK exposes HTTP status code
    if hasattr(e, "status_code"):
        status_code = int(getattr(e, "status_code"))
        return status_code == code

    return False


def map_gumnut_error(e: Exception, context: str) -> HTTPException:
    """
    Map Gumnut SDK exceptions to appropriate HTTP exceptions.

    Args:
        e: The exception from the Gumnut SDK
        context: Context string describing what operation failed

    Returns:
        HTTPException with appropriate status code and detail message
    """
    # Rate limit errors from photos-api must never reach Immich clients,
    # which have no 429 handling and would break (sync failures, broken
    # thumbnails). Map to 502 so the client sees an upstream error instead.
    if isinstance(e, RateLimitError):
        log_upstream_response(
            logger,
            context=context,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message="SDK retries exhausted for rate-limited request",
        )
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{context}: Upstream temporarily unavailable",
        )

    msg = str(e)

    # Try to extract clean message from SDK exception body
    detail = None
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message") or body.get("error")

    if not detail:
        detail = msg

    # If the SDK exposes HTTP status, use it
    if hasattr(e, "status_code"):
        code = int(getattr(e, "status_code"))
        log_upstream_response(
            logger,
            context=context,
            status_code=code,
            message=f"Gumnut SDK error in {context}: {msg}",
            extra={"error_detail": str(detail)[:500]},
        )
        return HTTPException(status_code=code, detail=detail)

    # Fallback to string matching for common HTTP errors
    # This is still brittle but better than duplicating everywhere
    code = get_upstream_status_code(e)
    if code == 404:
        log_upstream_response(
            logger,
            context=context,
            status_code=code,
            message=f"Gumnut SDK error in {context}: {msg}",
        )
        return HTTPException(status_code=404, detail=f"{context}: Not found")
    elif code == 401:
        log_upstream_response(
            logger,
            context=context,
            status_code=code,
            message=f"Gumnut SDK error in {context}: {msg}",
        )
        return HTTPException(status_code=401, detail=f"{context}: Invalid API key")
    elif code == 403:
        log_upstream_response(
            logger,
            context=context,
            status_code=code,
            message=f"Gumnut SDK error in {context}: {msg}",
        )
        return HTTPException(status_code=403, detail=f"{context}: Access denied")
    elif code == 400:
        log_upstream_response(
            logger,
            context=context,
            status_code=code,
            message=f"Gumnut SDK error in {context}: {msg}",
        )
        return HTTPException(status_code=400, detail=f"{context}: Bad request")

    # Final fallback to 500
    log_upstream_response(
        logger,
        context=context,
        status_code=500,
        message=f"Gumnut SDK error in {context}: {msg}",
    )
    return HTTPException(status_code=500, detail=f"{context}: {msg}")
