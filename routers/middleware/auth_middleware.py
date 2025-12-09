import logging

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from routers.utils.gumnut_client import get_refreshed_token, clear_refreshed_token
from services.session_store import get_session_store
from utils.jwt_encryption import JWTEncryptionError

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware that handles session token extraction and JWT refresh for all requests.

    This middleware:
    1. Detects client type (web vs mobile)
    2. Extracts session token from cookies (web) or Authorization header (mobile)
    3. Looks up the session in Redis and decrypts the stored JWT
    4. Stores the JWT in request.state for dependency injection
    5. Handles JWT refresh from backend by updating stored JWT in session
    """

    COOKIE_NAME = "immich_access_token"
    AUTH_HEADER = "authorization"
    REFRESH_HEADER = "x-new-access-token"

    # Endpoints that don't require authentication
    UNAUTHENTICATED_PATHS = {
        "/api/oauth/authorize",
        "/api/oauth/callback",
        "/api/oauth/mobile-redirect",
        "/api/auth/login",
        "/api/server/ping",
    }

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    def _invalid_token_response(self) -> JSONResponse:
        """Return a 401 response for invalid user token."""
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid user token"},
        )

    async def dispatch(self, request: Request, call_next):
        """
        Process the request to extract session token, look up JWT, and handle refresh.

        Args:
            request: The incoming HTTP request
            call_next: The next middleware or endpoint handler

        Returns:
            Response with potentially updated cookies or headers
        """
        path = request.url.path

        # Clear any stale refreshed token from previous requests
        clear_refreshed_token()

        # Skip auth for unauthenticated endpoints
        if path in self.UNAUTHENTICATED_PATHS:
            return await call_next(request)

        # Detect client type and extract session token
        session_token = None
        is_web_client = False

        # Check for Authorization header (standard Bearer token)
        auth_header = request.headers.get(self.AUTH_HEADER)
        if auth_header and auth_header.lower().startswith("bearer "):
            session_token = auth_header[7:]  # Remove "Bearer " prefix
            is_web_client = False
        # Check for Immich mobile client custom header
        elif "x-immich-user-token" in request.headers:
            session_token = request.headers.get("x-immich-user-token")
            is_web_client = False
        # Check for cookie (web client)
        elif self.COOKIE_NAME in request.cookies:
            session_token = request.cookies[self.COOKIE_NAME]
            is_web_client = True
        else:
            logger.warning(
                "No session token found in request",
                extra={
                    "path": path,
                    "cookies": list(request.cookies.keys()),
                },
            )

        # Look up session and decrypt JWT
        jwt_token = None
        if session_token:
            try:
                session_store = await get_session_store()
                session = await session_store.get_by_id(session_token)

                if session:
                    jwt_token = session.get_jwt()
                else:
                    logger.warning(
                        "Session not found for token",
                        extra={"path": path},
                    )
                    return self._invalid_token_response()
            except JWTEncryptionError:
                logger.error(
                    "Failed to decrypt JWT from session",
                    extra={"path": path},
                    exc_info=True,
                )
                return self._invalid_token_response()
            except Exception:
                logger.error(
                    "Failed to look up session",
                    extra={"path": path},
                    exc_info=True,
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": "Authentication service temporarily unavailable"
                    },
                )

        # Store in request state for dependency injection
        request.state.jwt_token = jwt_token
        request.state.session_token = session_token
        request.state.is_web_client = is_web_client

        # Call the endpoint handler
        response: Response = await call_next(request)

        # Check if Gumnut backend returned a refreshed token
        # The response hook in gumnut_client.py captures this from backend responses
        refreshed_token = get_refreshed_token()

        if refreshed_token and session_token:
            # Update the stored JWT in the session (session token stays the same)
            try:
                session_store = await get_session_store()
                await session_store.update_stored_jwt(session_token, refreshed_token)
            except Exception:
                logger.error(
                    "Failed to update stored JWT after refresh",
                    extra={"path": path},
                    exc_info=True,
                )

            # Always strip the refresh header - clients don't need it since their
            # session token remains valid (the JWT refresh is internal)
            if self.REFRESH_HEADER in response.headers:
                del response.headers[self.REFRESH_HEADER]

            # Clear the stored token after handling to prevent leakage to subsequent requests
            clear_refreshed_token()

        return response
