import asyncio

import httpx
from contextvars import ContextVar
from dataclasses import dataclass
from fastapi import HTTPException, Request, status
from gumnut import AsyncGumnut

from config.settings import get_settings

# Token Refresh Handling
# ----------------------
# This module handles JWT token refreshing from the Gumnut API. When a token is
# refreshed, the Gumnut API returns a new token in the 'x-new-access-token'
# response header. The auth middleware persists that token into the requesting
# user's session.
#
# The refreshed token must be isolated per request: a shared store would let one
# user's refreshed JWT be persisted into another user's session under concurrent
# load, granting cross-account access. We isolate it with a per-request *mutable
# holder* installed on a ContextVar:
#
#   - The middleware calls init_refresh_token_holder() before invoking the
#     downstream handler. ContextVar values set before the downstream call
#     propagate *into* the handler/response-hook context.
#   - The httpx response hook (running inside that context) mutates the same
#     holder object via set_refreshed_token().
#   - The middleware reads the token back via get_refreshed_token() after the
#     handler returns. Because the value read is a mutation of a shared object —
#     not a ContextVar.set() inside the handler — it is visible even though
#     Starlette's BaseHTTPMiddleware does not propagate ContextVar writes back
#     out of the downstream call.
#
# Each request installs its own holder, so concurrent requests on the same event
# loop thread can never observe each other's refreshed tokens.


@dataclass
class _RefreshTokenHolder:
    token: str | None = None


# Per-request token holder (see the Token Refresh Handling comment above).
_refresh_holder_var: ContextVar[_RefreshTokenHolder | None] = ContextVar(
    "refresh_token_holder", default=None
)

_shared_http_client: httpx.AsyncClient | None = None


def init_refresh_token_holder() -> None:
    """Install a fresh per-request refreshed-token holder.

    Must be called by the auth middleware before invoking the downstream handler
    so the response hook and the middleware share one per-request holder object.
    """
    _refresh_holder_var.set(_RefreshTokenHolder())


def _get_or_create_holder() -> _RefreshTokenHolder:
    holder = _refresh_holder_var.get()
    if holder is None:
        # No holder was installed for this context (e.g. a direct call outside
        # the request lifecycle). Install one so the token isn't silently lost
        # within the current context. This still cannot leak across requests:
        # the holder lives only in this context's ContextVar.
        holder = _RefreshTokenHolder()
        _refresh_holder_var.set(holder)
    return holder


def get_refreshed_token() -> str | None:
    """Return the refreshed token captured for the current request, if any."""
    holder = _refresh_holder_var.get()
    return holder.token if holder is not None else None


def set_refreshed_token(token: str) -> None:
    """Record a refreshed token on the current request's holder."""
    _get_or_create_holder().token = token


def clear_refreshed_token() -> None:
    """Clear the refreshed token on the current request's holder, if present.

    No production code calls this — request isolation comes from each request
    installing its own holder, not from clearing. Kept as a test reset helper.
    """
    holder = _refresh_holder_var.get()
    if holder is not None:
        holder.token = None


async def _response_hook(response: httpx.Response) -> None:
    """
    HTTP response hook that captures refreshed tokens from Gumnut API responses.

    When the Gumnut API refreshes a token, it returns the new token in the
    'x-new-access-token' header. This hook records that token on the current
    request's refreshed-token holder (see the Token Refresh Handling comment at
    the top of this module) for later retrieval
    by the auth middleware.

    Args:
        response: The httpx Response object from the Gumnut API
    """
    token = response.headers.get("x-new-access-token")
    if token:
        set_refreshed_token(token)


_client_lock = asyncio.Lock()


async def get_shared_http_client() -> httpx.AsyncClient:
    """
    Get or create the shared async HTTP client for Gumnut connections.

    This client is shared across all requests for connection pooling.
    Each Gumnut instance has its own JWT but shares the connection pool.

    The client is configured with a response hook to capture token refreshes
    from the Gumnut API.

    Returns:
        httpx.AsyncClient: Shared HTTP client for connection pooling with response hook
    """
    global _shared_http_client
    if _shared_http_client is None:
        async with _client_lock:
            if _shared_http_client is None:
                _shared_http_client = httpx.AsyncClient(
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
        await _shared_http_client.aclose()
        _shared_http_client = None


async def get_gumnut_client(jwt_token: str) -> AsyncGumnut:
    """
    Create and return a configured AsyncGumnut client instance with the given JWT.

    Uses a shared HTTP client for connection pooling (stateless).
    Each client instance has its own JWT but shares the connection pool.
    Configures max_retries=3 for SDK-level retry of 429s and transient errors.

    Args:
        jwt_token: JWT token for authenticated requests

    Returns:
        AsyncGumnut: Configured async Gumnut client instance with user's JWT
    """
    settings = get_settings()

    return AsyncGumnut(
        api_key=jwt_token,
        base_url=settings.gumnut_api_base_url,
        max_retries=3,
        http_client=await get_shared_http_client(),
    )


async def get_authenticated_gumnut_client(request: Request) -> AsyncGumnut:
    """
    Dependency that provides an authenticated AsyncGumnut client for the current request.

    Extracts the JWT from request.state (set by auth middleware) and creates
    an AsyncGumnut client instance with that JWT.

    Args:
        request: FastAPI request object containing state set by middleware

    Returns:
        AsyncGumnut: Authenticated async Gumnut client instance for the current user

    Raises:
        HTTPException: 401 if no JWT is present in request state
    """
    jwt_token = getattr(request.state, "jwt_token", None)

    if not jwt_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    return await get_gumnut_client(jwt_token)


async def get_authenticated_gumnut_client_optional(
    request: Request,
) -> AsyncGumnut | None:
    """
    Dependency that provides an authenticated AsyncGumnut client for the current request
    if a JWT is present. Otherwise, returns None without raising an exception.

    Used during logout to prevent errors when no JWT is present.

    Args:
        request: FastAPI request object containing state set by middleware

    Returns:
        AsyncGumnut | None: Authenticated async Gumnut client, or None if no JWT
    """
    jwt_token = getattr(request.state, "jwt_token", None)

    if jwt_token:
        return await get_gumnut_client(jwt_token)

    return None


async def get_unauthenticated_gumnut_client() -> AsyncGumnut:
    """
    Dependency that provides an unauthenticated AsyncGumnut client for OAuth operations.

    This is used for OAuth endpoints that don't require authentication (like
    starting OAuth flow or handling callbacks) but still need to communicate
    with the Gumnut backend.

    Returns:
        AsyncGumnut: Unauthenticated async Gumnut client instance
    """
    settings = get_settings()

    return AsyncGumnut(
        base_url=settings.gumnut_api_base_url,
        max_retries=3,
        http_client=await get_shared_http_client(),
    )
