"""Exception handlers for the immich-adapter."""

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse


async def _immich_http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Format HTTP errors in Immich's expected format."""
    try:
        error_name = HTTPStatus(exc.status_code).phrase
    except ValueError:
        error_name = "Error"

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "message": exc.detail,
            "statusCode": exc.status_code,
            "error": error_name,
        },
    )


def configure_exception_handlers(app: FastAPI) -> None:
    """Register global exception handlers for the application.

    This function registers handlers that convert application exceptions
    to appropriate HTTP responses. Add new exception handlers here as needed.
    """
    app.add_exception_handler(HTTPException, _immich_http_exception_handler)  # type: ignore[arg-type]
