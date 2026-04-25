"""
Error mapping utilities for handling Gumnut SDK exceptions.

Most adapter routes do not need to map SDK errors at all — `GumnutError` and
its subclasses are caught by the global handler in `config/exceptions.py` and
turned into Immich-shaped HTTP responses there.

Use `map_gumnut_error` only when a call site needs to enrich the upstream log
record with structured context that the global handler does not have (e.g.
upload paths logging filename / device ids / `exc_info`).
"""

import logging
from typing import Any

from fastapi import HTTPException, status
from gumnut import APIStatusError, RateLimitError

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
    exc_info: bool = False,
) -> None:
    """Log an upstream response/error using the shared status-to-level policy."""
    log_extra: dict[str, Any] = dict(extra or {})
    # Helper fields are authoritative and must not be overridden by caller extra.
    log_extra["context"] = context
    log_extra["status_code"] = status_code

    logger_obj.log(
        upstream_status_log_level(status_code),
        message,
        extra=log_extra,
        exc_info=exc_info,
    )


def map_gumnut_error(
    e: Exception,
    context: str,
    *,
    extra: dict[str, Any] | None = None,
    exc_info: bool = False,
) -> HTTPException:
    """
    Map a Gumnut SDK exception to an HTTPException, logging at the upstream
    severity policy.

    Prefer letting SDK errors bubble to the global GumnutError handler
    (`config/exceptions.py`). Use this helper only when the call site has
    enriching log context (filename, device ids, etc.) that the global handler
    cannot provide.

    Args:
        e: The exception from the Gumnut SDK
        context: Context string describing what operation failed
        extra: Optional structured fields merged into the upstream log record.
            Caller-supplied "context" / "status_code" keys are overridden by
            this helper's authoritative values.
        exc_info: When True, attach the current exception traceback to the
            emitted log record.

    Returns:
        HTTPException with appropriate status code and detail message
    """
    # Rate-limit errors must never surface as 429 to Immich clients (no 429
    # handling on the client side; would break sync, thumbnails, uploads).
    if isinstance(e, RateLimitError):
        log_upstream_response(
            logger,
            context=context,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message="SDK retries exhausted for rate-limited request",
            extra=extra,
            exc_info=exc_info,
        )
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{context}: Upstream temporarily unavailable",
        )

    if isinstance(e, APIStatusError):
        body = e.body
        detail: str | None = None
        if isinstance(body, dict):
            for key in ("detail", "message", "error"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    detail = value
                    break
        if not detail:
            detail = e.message or f"Upstream HTTP {e.status_code}"

        log_extra: dict[str, Any] = dict(extra or {})
        log_extra["error_detail"] = str(detail)[:500]
        log_upstream_response(
            logger,
            context=context,
            status_code=e.status_code,
            message=f"Gumnut SDK error in {context}: {e.message}",
            extra=log_extra,
            exc_info=exc_info,
        )
        return HTTPException(status_code=e.status_code, detail=detail)

    # Non-SDK exception (transport error, programmer error, etc.) — map to 500.
    log_upstream_response(
        logger,
        context=context,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        message=f"Unhandled error in {context}: {e}",
        extra=extra,
        exc_info=exc_info,
    )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"{context}: {e}",
    )
