import httpx
import threading
from fastapi import HTTPException, Request, status
from gumnut import Gumnut

from config.settings import get_settings

# Thread-local storage for refreshed tokens from Gumnut backend responses
#
# Note: We use threading.local() instead of contextvars.ContextVar here, even though
# contextvars would be more "correct" for async code. The reason is that Starlette's
# TestClient doesn't preserve context variables across the middleware → endpoint
# boundary during testing. The TestClient uses a synchronous wrapper that breaks
# the async context chain.
#
# In production, both approaches would work fine because real HTTP requests maintain
# a consistent async context. Thread-local storage works in both environments:
# - TestClient runs everything in a single thread, so thread-locals are shared
# - Real FastAPI requests typically run in a single thread (even when async)
# - Thread-locals survive across the async context boundaries that TestClient creates
#
# This is a common compromise when building FastAPI applications - using thread-locals
# to accommodate testing frameworks while still accurately testing real behavior.
_thread_local = threading.local()

_shared_http_client: httpx.Client | None = None


def _capture_refresh_token_hook(response: httpx.Response) -> None:
    """
    Response hook that captures x-new-access-token headers from Gumnut backend.

    When the Gumnut backend refreshes a JWT token, it returns the new token
    in the x-new-access-token response header. This hook captures that header
    and stores it in thread-local storage so it can be propagated to the client.

    Args:
        response: The httpx Response object from Gumnut backend
    """
    refresh_header = "x-new-access-token"
    if refresh_header in response.headers:
        new_token = response.headers[refresh_header]
        _thread_local.refreshed_token = new_token


def get_shared_http_client() -> httpx.Client:
    """
    Get or create the shared HTTP client for Gumnut connections.

    This client is shared across all requests for connection pooling.
    Each Gumnut instance has its own JWT but shares the connection pool.

    The client includes a response hook that captures token refresh headers
    from the Gumnut backend.

    Returns:
        httpx.Client: Shared HTTP client for connection pooling
    """
    global _shared_http_client

    if _shared_http_client is None:
        _shared_http_client = httpx.Client(
            timeout=30.0,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
            ),
            event_hooks={"response": [_capture_refresh_token_hook]},
        )

    return _shared_http_client


def get_gumnut_client(jwt_token: str) -> Gumnut:
    """
    Create and return a configured Gumnut client instance with the given JWT.

    Uses a shared HTTP client for connection pooling (stateless).
    Each client instance has its own JWT but shares the connection pool.

    Args:
        jwt_token: JWT token for authenticated requests

    Returns:
        Gumnut: Configured Gumnut client instance with user's JWT
    """
    settings = get_settings()

    return Gumnut(
        api_key=jwt_token,
        base_url=settings.gumnut_api_base_url,
        http_client=get_shared_http_client(),
    )


def get_refreshed_token() -> str | None:
    """
    Get the refreshed token captured from the most recent Gumnut backend response.

    This function retrieves the token stored by the response hook when the
    Gumnut backend returns a refreshed JWT in the x-new-access-token header.

    Returns:
        str | None: The refreshed token if one was captured, None otherwise
    """
    return getattr(_thread_local, "refreshed_token", None)


def clear_refreshed_token() -> None:
    """
    Clear the refreshed token from thread-local storage.

    This should be called at the start of each request to ensure stale tokens
    from previous requests don't leak into the current request.
    """
    _thread_local.refreshed_token = None


async def get_authenticated_gumnut_client(request: Request) -> Gumnut:
    """
    Dependency that provides an authenticated Gumnut client for the current request.

    Extracts the JWT from request.state (set by auth middleware) and creates
    a Gumnut client instance with that JWT.

    Args:
        request: FastAPI request object containing state set by middleware

    Returns:
        Gumnut: Authenticated Gumnut client instance for the current user

    Raises:
        HTTPException: 401 if no JWT is present in request state
    """
    jwt_token = getattr(request.state, "jwt_token", None)

    if not jwt_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    return get_gumnut_client(jwt_token)


async def get_unauthenticated_gumnut_client() -> Gumnut:
    """
    Dependency that provides an unauthenticated Gumnut client for OAuth operations.

    This is used for OAuth endpoints that don't require authentication (like
    starting OAuth flow or handling callbacks) but still need to communicate
    with the Gumnut backend.

    Returns:
        Gumnut: Unauthenticated Gumnut client instance
    """
    settings = get_settings()

    return Gumnut(
        base_url=settings.gumnut_api_base_url,
    )
