import logging

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from routers.utils.gumnut_client import get_refreshed_token, clear_refreshed_token
from routers.utils.cookies import update_access_token_cookie

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware that handles JWT extraction and token refresh for all requests.

    This middleware:
    1. Detects client type (web vs mobile)
    2. Extracts JWT from cookies (web) or Authorization header (mobile)
    3. Stores JWT in request.state for dependency injection
    4. Handles token refresh responses from backend
    5. Updates cookies (web) or passes headers (mobile) for refreshed tokens
    """

    COOKIE_NAME = "immich_access_token"
    AUTH_HEADER = "authorization"
    REFRESH_HEADER = "x-new-access-token"

    # Endpoints that don't require authentication
    UNAUTHENTICATED_PATHS = {
        "/api/oauth/authorize",
        "/api/oauth/callback",
        "/api/auth/login",
    }

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        """
        Process the request to extract JWT and handle the response for token refresh.

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

        # Detect client type and extract JWT
        jwt_token = None
        is_web_client = False

        # Check for Authorization header (mobile client)
        auth_header = request.headers.get(self.AUTH_HEADER)
        if auth_header and auth_header.lower().startswith("bearer "):
            jwt_token = auth_header[7:]  # Remove "Bearer " prefix
            is_web_client = False
        # Check for cookie (web client)
        elif self.COOKIE_NAME in request.cookies:
            jwt_token = request.cookies[self.COOKIE_NAME]
            is_web_client = True
        else:
            logger.warning(
                "No JWT found in request",
                extra={
                    "path": path,
                    "cookies": list(request.cookies.keys()),
                },
            )

        # Store JWT in request state for dependency injection
        request.state.jwt_token = jwt_token
        request.state.is_web_client = is_web_client

        # Call the endpoint handler
        response: Response = await call_next(request)

        # Check if Gumnut backend returned a refreshed token
        # The response hook in gumnut_client.py captures this from backend responses
        refreshed_token = get_refreshed_token()

        if refreshed_token:
            if is_web_client:
                # Web client: Update cookie and strip header to prevent token exposure
                update_access_token_cookie(
                    response, refreshed_token, request.url.scheme == "https"
                )
                # Remove the header since web client uses cookies, not headers
                # This prevents accidental token exposure in response headers
                if self.REFRESH_HEADER in response.headers:
                    del response.headers[self.REFRESH_HEADER]
            else:
                # Mobile client: Add header so client can read the new token
                response.headers[self.REFRESH_HEADER] = refreshed_token

            # Clear the stored token after handling to prevent leakage to subsequent requests
            clear_refreshed_token()

        return response
