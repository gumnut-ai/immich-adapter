"""Exception handlers for the immich-adapter."""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from gumnut import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    GumnutError,
    RateLimitError,
)

from routers.utils.error_mapping import log_upstream_response, logger


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


def _detail_from_status_error(exc: APIStatusError) -> str:
    """Extract a clean detail message from a Gumnut SDK status error."""
    body = exc.body
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return exc.message or f"Upstream HTTP {exc.status_code}"


def _route_context(request: Request) -> str:
    """Return a stable context string for log records derived from the route."""
    route = request.scope.get("route")
    name = getattr(route, "name", None) or getattr(route, "path", None)
    return name or f"{request.method} {request.url.path}"


async def _gumnut_error_handler(request: Request, exc: GumnutError) -> JSONResponse:
    """Map any Gumnut SDK exception to an Immich-shaped JSON response.

    Mapping policy (all mappings treat the adapter as an HTTP gateway in
    front of photos-api, so 5xx-from-upstream-or-transport is reported as
    502 Bad Gateway, per RFC 9110):

    - RateLimitError (429) → 502 "Upstream temporarily unavailable".
      Immich mobile/web clients have no 429 handling; passing 429 through
      breaks sync, thumbnails, and uploads. 502 is correct (gateway can't
      fulfil); 503 would imply the adapter itself is overloaded.
    - APIStatusError (other 4xx/5xx) → pass through the upstream code
      with detail extracted from body. Status codes like 400/403/404/409/
      422 carry semantic information that Immich clients can act on, so
      transparent forwarding is the default. (Caveat: a 401 from photos-
      api on adapter-internal-auth calls means the adapter's JWT expired,
      not the client's session — those call sites must NOT use this
      handler. See `services/streaming_upload.py` for the 401→502 case.)
    - APIResponseValidationError → 502 "Upstream returned invalid
      response". Upstream replied 2xx but the body didn't match the SDK's
      schema; from the client's perspective the gateway can't fulfil.
    - APIConnectionError / APITimeoutError → 502 "Upstream unreachable".
      Transport failure with no HTTP response from upstream — canonical
      Bad Gateway scenario.
    - Generic GumnutError → 500 "Internal error". An SDK error that
      doesn't match any of the above is an adapter bug or unhandled SDK
      state, not a gateway condition — surface as 500.

    Routes that need to enrich the log record with call-site context
    (e.g. upload paths) should catch the SDK exception themselves and use
    `map_gumnut_error(extra=..., exc_info=True)` instead of letting the
    error reach this handler.
    """
    context = _route_context(request)

    if isinstance(exc, RateLimitError):
        log_upstream_response(
            logger,
            context=context,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message="SDK retries exhausted for rate-limited request",
            exc_info=True,
        )
        return _immich_response(
            status.HTTP_502_BAD_GATEWAY,
            "Upstream temporarily unavailable",
        )

    if isinstance(exc, APIStatusError):
        detail = _detail_from_status_error(exc)
        log_extra: dict[str, Any] = {"error_detail": detail[:500]}
        log_upstream_response(
            logger,
            context=context,
            status_code=exc.status_code,
            message=f"Gumnut SDK error in {context}: {exc.message}",
            extra=log_extra,
            exc_info=True,
        )
        return _immich_response(exc.status_code, detail)

    if isinstance(exc, APIResponseValidationError):
        log_upstream_response(
            logger,
            context=context,
            status_code=exc.status_code,
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
