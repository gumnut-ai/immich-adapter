import httpx
import threading
from contextvars import ContextVar
from fastapi import HTTPException, Request, status
from gumnut import Gumnut

from config.settings import get_settings

# Token Refresh Handling
# ----------------------
# This module handles JWT token refreshing from the Gumnut API. When a token is refreshed,
# the Gumnut API returns a new token in the 'x-new-access-token' response header.
#
# To safely propagate refreshed tokens in async/concurrent environments, we use a two-tier
# approach:
#
# 1. **ContextVar (Primary)**: Used for async request contexts. ContextVars are isolated
#    per async task, preventing token collisions between concurrent requests on the same
#    thread. This is the correct approach for production async environments.
#
# 2. **threading.local (Fallback)**: Used for synchronous test environments (like FastAPI's
#    TestClient) where context propagation doesn't work. Multiple concurrent requests on
#    the same thread can still interleave with thread-local, but this is acceptable for
#    tests where requests are typically sequential.
#
# The response hook sets both storage mechanisms, and the getter checks ContextVar first,
# then falls back to thread-local. This ensures correct behavior in both production
# (async) and test (sync) environments.

# Async-safe token storage (primary)
_refreshed_token_var: ContextVar[str | None] = ContextVar(
    "refreshed_token", default=None
)

# Thread-local token storage (fallback for TestClient)
_thread_local = threading.local()

_shared_http_client: httpx.Client | None = None


def get_refreshed_token() -> str | None:
    """
    Get the refreshed token from either ContextVar or thread-local storage.

    Checks ContextVar first (for async contexts), then falls back to thread-local
    (for sync test contexts like TestClient).

    Returns:
        str | None: The refreshed token if available, None otherwise
    """
    # Try ContextVar first (async-safe)
    token = _refreshed_token_var.get()
    if token is not None:
        return token

    # Fall back to thread-local (for TestClient)
    return getattr(_thread_local, "refreshed_token", None)


def set_refreshed_token(token: str) -> None:
    """
    Store a refreshed token in both ContextVar and thread-local storage.

    Sets both storage mechanisms to ensure the token is available in both
    async (production) and sync (TestClient) environments.

    Args:
        token: The refreshed JWT token to store
    """
    _refreshed_token_var.set(token)
    _thread_local.refreshed_token = token


def clear_refreshed_token() -> None:
    """
    Clear the refreshed token from both storage mechanisms.

    Should be called after the token has been propagated to the response
    to prevent token leakage between requests.
    """
    _refreshed_token_var.set(None)
    _thread_local.refreshed_token = None


def _response_hook(response: httpx.Response) -> None:
    """
    HTTP response hook that captures refreshed tokens from Gumnut API responses.

    When the Gumnut API refreshes a token, it returns the new token in the
    'x-new-access-token' header. This hook captures that token and stores it
    in both ContextVar and thread-local storage for later retrieval by middleware.

    Args:
        response: The httpx Response object from the Gumnut API
    """
    token = response.headers.get("x-new-access-token")
    if token:
        set_refreshed_token(token)


_client_lock = threading.Lock()


def get_shared_http_client() -> httpx.Client:
    """
    Get or create the shared HTTP client for Gumnut connections.

    This client is shared across all requests for connection pooling.
    Each Gumnut instance has its own JWT but shares the connection pool.

    The client is configured with a response hook to capture token refreshes
    from the Gumnut API.

    Returns:
        httpx.Client: Shared HTTP client for connection pooling with response hook
    """
    global _shared_http_client
    if _shared_http_client is None:
        with _client_lock:
            if _shared_http_client is None:
                _shared_http_client = httpx.Client(
                    timeout=30.0,
                    limits=httpx.Limits(
                        max_connections=100, max_keepalive_connections=20
                    ),
                    event_hooks={"response": [_response_hook]},
                )
    return _shared_http_client


async def close_shared_http_client() -> None:
    """
    Close and clean up the shared HTTP client.
    Should be called on application shutdown to release resources.
    """
    global _shared_http_client
    if _shared_http_client is not None:
        _shared_http_client.close()
        _shared_http_client = None


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
