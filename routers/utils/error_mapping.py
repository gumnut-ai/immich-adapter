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
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

from fastapi import HTTPException, status
from gumnut import (
    APIStatusError,
    AuthenticationError,
    GumnutError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

# Truncation cap for the `error_detail` field on upstream log records — keeps
# Sentry / log search tractable while preserving enough context to debug.
ERROR_DETAIL_MAX_CHARS = 500

E = TypeVar("E", bound=Enum)


@dataclass(frozen=True)
class StainlessErrorClassification:
    """Shared classification result for common Stainless SDK exceptions."""

    client_status_code: int
    log_status_code: int
    log_message: str
    response_detail: str
    error_detail_for_log: str | None = None


def truncated_error_detail(exc: Exception) -> str:
    """Stringify and truncate an exception for the `error_detail` log field."""
    return str(exc)[:ERROR_DETAIL_MAX_CHARS]


def extract_detail_from_status_error(exc: APIStatusError) -> str:
    """Extract a clean detail message from a Gumnut SDK status error.

    Tries `body.detail`, then `body.message`, then `body.error`, then
    `exc.message`, then a synthesized fallback. Used by both the global
    GumnutError handler and `map_gumnut_error`.
    """
    body = exc.body
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return exc.message or f"Upstream HTTP {exc.status_code}"


def classify_stainless_error(
    exc: Exception,
    *,
    context: str,
    rate_limit_response_detail: str,
) -> StainlessErrorClassification | None:
    """Classify common Stainless SDK errors shared by multiple call sites.

    Returns `None` when the error should be handled by caller-specific logic.
    """
    # Rate-limit errors must never surface as 429 to Immich clients (no 429
    # handling on the client side; would break sync, thumbnails, uploads).
    if isinstance(exc, RateLimitError):
        return StainlessErrorClassification(
            client_status_code=status.HTTP_502_BAD_GATEWAY,
            log_status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            log_message="SDK retries exhausted for rate-limited request",
            response_detail=rate_limit_response_detail,
        )

    if isinstance(exc, APIStatusError):
        detail = extract_detail_from_status_error(exc)
        return StainlessErrorClassification(
            client_status_code=exc.status_code,
            log_status_code=exc.status_code,
            log_message=f"Gumnut SDK error in {context}: {exc.message}",
            response_detail=detail,
            error_detail_for_log=detail[:ERROR_DETAIL_MAX_CHARS],
        )

    return None


def classify_bulk_item_error(exc: APIStatusError, enum_cls: type[E]) -> E:
    """Classify a per-item APIStatusError as an `Error1` / `BulkIdErrorReason` value.

    Maps to the canonical `not_found` / `no_permission` / `unknown` buckets
    on the supplied enum. Per-endpoint nuances (e.g. mapping `ConflictError`
    to `duplicate`) are layered by the caller via an earlier `except`.
    """
    if isinstance(exc, NotFoundError):
        return enum_cls["not_found"]
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return enum_cls["no_permission"]
    return enum_cls["unknown"]


def log_bulk_transport_error(
    logger_obj: logging.Logger,
    *,
    context: str,
    exc: GumnutError,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log a per-item transport / schema-mismatch error from a bulk endpoint.

    Use after catching a non-APIStatusError `GumnutError` inside a per-item
    loop. The caller is responsible for recording the failure on the
    response (e.g. appending an `unknown` `BulkIdResponseDto`) — this just
    centralizes the log shape (502 client severity + truncated detail) so
    every bulk endpoint emits the same field set.
    """
    log_extra: dict[str, Any] = dict(extra or {})
    log_extra["error_detail"] = truncated_error_detail(exc)
    log_upstream_response(
        logger_obj,
        context=context,
        status_code=status.HTTP_502_BAD_GATEWAY,
        message=f"Transport error in {context}",
        extra=log_extra,
    )


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
    classification = classify_stainless_error(
        e,
        context=context,
        rate_limit_response_detail=f"{context}: Upstream temporarily unavailable",
    )
    if classification is not None:
        log_extra: dict[str, Any] = dict(extra or {})
        if classification.error_detail_for_log is not None:
            log_extra["error_detail"] = classification.error_detail_for_log

        log_upstream_response(
            logger,
            context=context,
            status_code=classification.log_status_code,
            message=classification.log_message,
            extra=log_extra or None,
            exc_info=exc_info,
        )
        return HTTPException(
            status_code=classification.client_status_code,
            detail=classification.response_detail,
        )

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
