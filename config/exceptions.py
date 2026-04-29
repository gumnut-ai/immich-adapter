"""Exception handlers for the immich-adapter."""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from gumnut import (
    APIConnectionError,
    APIResponseValidationError,
    GumnutError,
)

from routers.utils.error_mapping import (
    classify_stainless_error,
    log_upstream_response,
    logger,
)


def _immich_response(status_code: int, message: str) -> JSONResponse:
    """Build a JSONResponse in Immich's expected error shape."""
    try:
        error_name = HTTPStatus(status_code).phrase
    except ValueError:
        error_name = "Error"
    return JSONResponse(
        status_code=status_code,
        content={
            "message": message,
            "statusCode": status_code,
            "error": error_name,
        },
    )


async def _immich_http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Format HTTP errors in Immich's expected format."""
    response = _immich_response(exc.status_code, str(exc.detail))
    if exc.headers:
        response.headers.update(exc.headers)
    return response


def _route_context(request: Request) -> str:
    """Return a stable context string for log records derived from the route."""
    route = request.scope.get("route")
    name = getattr(route, "name", None) or getattr(route, "path", None)
    return name or f"{request.method} {request.url.path}"


async def _gumnut_error_handler(request: Request, exc: GumnutError) -> JSONResponse:
    """Map any Gumnut SDK exception to an Immich-shaped JSON response.

    Routes that need to enrich the log record with call-site context
    (e.g. upload paths) should catch the SDK exception themselves and use
    `map_gumnut_error(extra=..., exc_info=True)` instead of letting the
    error reach this handler.
    """
    context = _route_context(request)

    classification = classify_stainless_error(
        exc,
        context=context,
        rate_limit_response_detail="Upstream temporarily unavailable",
    )
    if classification is not None:
        log_extra: dict[str, Any] = {}
        if classification.error_detail_for_log is not None:
            log_extra["error_detail"] = classification.error_detail_for_log

        log_upstream_response(
            logger,
            context=context,
            status_code=classification.log_status_code,
            message=classification.log_message,
            extra=log_extra or None,
            exc_info=True,
        )
        return _immich_response(
            classification.client_status_code,
            classification.response_detail,
        )

    if isinstance(exc, APIResponseValidationError):
        # Schema mismatch is a contract bug — log at 502 ERROR severity, not
        # the upstream 2xx (which would demote to INFO).
        log_upstream_response(
            logger,
            context=context,
            status_code=status.HTTP_502_BAD_GATEWAY,
            message=f"Gumnut SDK returned invalid response in {context}: {exc.message}",
            exc_info=True,
        )
        return _immich_response(
            status.HTTP_502_BAD_GATEWAY,
            "Upstream returned invalid response",
        )

    if isinstance(exc, APIConnectionError):
        log_upstream_response(
            logger,
            context=context,
            status_code=status.HTTP_502_BAD_GATEWAY,
            message=f"Gumnut SDK connection error in {context}: {exc.message}",
            exc_info=True,
        )
        return _immich_response(
            status.HTTP_502_BAD_GATEWAY,
            "Upstream unreachable",
        )

    # Generic GumnutError fallback (no HTTP status, not transport).
    log_upstream_response(
        logger,
        context=context,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        message=f"Unhandled Gumnut SDK error in {context}: {exc}",
        exc_info=True,
    )
    return _immich_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "Internal error",
    )


def configure_exception_handlers(app: FastAPI) -> None:
    """Register global exception handlers for the application.

    This function registers handlers that convert application exceptions
    to appropriate HTTP responses. Add new exception handlers here as needed.
    """
    app.add_exception_handler(HTTPException, _immich_http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(GumnutError, _gumnut_error_handler)  # type: ignore[arg-type]
