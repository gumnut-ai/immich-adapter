import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial

import httpx
from contextvars import ContextVar
from dataclasses import dataclass
from fastapi import Depends, HTTPException, Request, status
from gumnut import AsyncGumnut, PermissionDeniedError

from config.settings import get_settings
from services.library_resolver import (
    LibraryCache,
    first_live_library_id,
    get_library_cache,
    is_library_not_found,
)

logger = logging.getLogger(__name__)

# Per-Request Scope
# -----------------
# The shared httpx response hook below sees every Gumnut SDK response but has
# no request object, so per-request state reaches it through a *mutable* object
# installed on a ContextVar by init_request_scope() before call_next. Code
# downstream mutates that object in place, which the middleware can read back
# afterwards — a ContextVar.set() inside the handler could not, because
# Starlette's BaseHTTPMiddleware does not propagate those writes back out. Each
# request installs its own scope, so concurrent requests on one event-loop
# thread never observe each other's state; sharing would let one user's
# refreshed JWT land in another user's session.
#
# The scope carries the refreshed JWT (returned in the 'x-new-access-token'
# response header, persisted into the session by the middleware) and the
# request's bound library plus how to forget it (see
# docs/architecture/adapter-architecture.md § Library scope), so the hook can
# drop a vanished library even from calls inside a streaming body, which the
# global exception handler never sees.


@dataclass
class LibraryScope:
    """The library a request's Gumnut calls are bound to and how to forget it."""

    library_id: str
    forget: Callable[[], Awaitable[None]]
    forgotten: bool = False


@dataclass
class _RequestScope:
    refreshed_token: str | None = None
    library: LibraryScope | None = None


_request_scope_var: ContextVar[_RequestScope | None] = ContextVar(
    "request_scope", default=None
)

_shared_http_client: httpx.AsyncClient | None = None


def init_request_scope() -> None:
    """Install a fresh per-request scope.

    Must be called by the auth middleware before invoking the downstream handler
    so the handler, the response hook, and the middleware share one object.
    """
    _request_scope_var.set(_RequestScope())


def _get_or_create_scope() -> _RequestScope:
    scope = _request_scope_var.get()
    if scope is None:
        # No scope for this context (e.g. a direct call outside the request
        # lifecycle). Install one; it lives only in this context's ContextVar,
        # so it still cannot leak across requests.
        scope = _RequestScope()
        _request_scope_var.set(scope)
    return scope


def get_refreshed_token() -> str | None:
    """Return the refreshed token captured for the current request, if any."""
    scope = _request_scope_var.get()
    return scope.refreshed_token if scope is not None else None


def set_refreshed_token(token: str) -> None:
    """Record a refreshed token on the current request's scope."""
    _get_or_create_scope().refreshed_token = token


def clear_refreshed_token() -> None:
    """Clear the refreshed token on the current request's scope, if present.

    No production code calls this — request isolation comes from each request
    installing its own scope, not from clearing. Kept as a test reset helper.
    """
    scope = _request_scope_var.get()
    if scope is not None:
        scope.refreshed_token = None


def bind_library_scope(library: LibraryScope | None) -> None:
    """Install (or clear, with ``None``) the current request's library scope."""
    _get_or_create_scope().library = library


def get_bound_library_id() -> str | None:
    """The library id bound for the current request, if one was resolved."""
    scope = _request_scope_var.get()
    return scope.library.library_id if scope and scope.library else None


async def forget_bound_library_if_gone(status_code: int, detail: str) -> None:
    """Drop the cached library when a Gumnut error says the bound one vanished.

    Non-throwing: callers sit inside SDK calls or upload error paths, and a
    cache failure must not turn the upstream error into something else.
    """
    scope = _request_scope_var.get()
    library = scope.library if scope is not None else None
    if library is None or library.forgotten:
        return
    if status_code != status.HTTP_404_NOT_FOUND or not is_library_not_found(
        detail, library.library_id
    ):
        return
    # Later calls in this request still carry the stale id; one drop is enough.
    library.forgotten = True
    logger.warning(
        "Bound library no longer available; dropping cached library",
        extra={"library_id": library.library_id},
    )
    try:
        await library.forget()
    except Exception:
        logger.error("Failed to drop cached library", exc_info=True)


async def _response_hook(response: httpx.Response) -> None:
    """Run on every Gumnut SDK response: record a refreshed token from the
    'x-new-access-token' header on the current request's scope, and drop the
    cached library when a 404 names the request's bound library (see the
    Per-Request Scope comment above).
    """
    token = response.headers.get("x-new-access-token")
    if token:
        set_refreshed_token(token)
    if response.status_code == status.HTTP_404_NOT_FOUND and get_bound_library_id():
        await response.aread()
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            return
        if isinstance(detail, str):
            await forget_bound_library_if_gone(response.status_code, detail)


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


async def get_gumnut_client(
    jwt_token: str, library_id: str | None = None
) -> AsyncGumnut:
    """
    Create and return a configured AsyncGumnut client instance with the given JWT.

    Uses a shared HTTP client for connection pooling (stateless).
    Each client instance has its own JWT but shares the connection pool.
    Configures max_retries=3 for SDK-level retry of 429s and transient errors.

    Args:
        jwt_token: JWT token for authenticated requests
        library_id: When set, sent as the ``library_id`` query parameter on
            every call. An explicit per-call value still wins.

    Returns:
        AsyncGumnut: Configured async Gumnut client instance with user's JWT
    """
    settings = get_settings()

    return AsyncGumnut(
        api_key=jwt_token,
        base_url=settings.gumnut_api_base_url,
        max_retries=3,
        http_client=await get_shared_http_client(),
        default_query={"library_id": library_id} if library_id else None,
    )


async def _resolve_library_id(
    request: Request, credential: str, cache: LibraryCache
) -> str | None:
    """Resolve the library for this request's credential, caching the result.

    ``credential`` is the session's JWT or the raw API key. ``None`` leaves the
    request unscoped and caches nothing.
    """
    session_token = getattr(request.state, "session_token", None)
    if session_token:
        library_id = getattr(request.state, "session_library_id", None)
        remember = partial(cache.remember_for_session, session_token)
        forget = partial(cache.forget_session, session_token)
    else:
        library_id = await cache.get_for_api_key(credential)
        remember = partial(cache.remember_for_api_key, credential)
        forget = partial(cache.forget_api_key, credential)

    if not library_id:
        unscoped = await get_gumnut_client(credential)
        try:
            libraries = await unscoped.libraries.list()
        except PermissionDeniedError:
            logger.warning(
                "Credential cannot list libraries; leaving calls unscoped",
                extra={"path": request.url.path},
            )
            return None
        library_id = first_live_library_id(libraries)
        if library_id is None:
            logger.info("User has no live library; leaving calls unscoped")
            return None
        await remember(library_id)

    bind_library_scope(LibraryScope(library_id=library_id, forget=forget))
    return library_id


async def get_authenticated_gumnut_client(
    request: Request, cache: LibraryCache = Depends(get_library_cache)
) -> AsyncGumnut:
    """
    Dependency that provides an authenticated AsyncGumnut client for the current request.

    Extracts the JWT from request.state (set by auth middleware), resolves the
    library the request acts on, and creates an AsyncGumnut client bound to
    both.

    Args:
        request: FastAPI request object containing state set by middleware
        cache: Per-credential cache of the resolved library

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

    library_id = await _resolve_library_id(request, jwt_token, cache)
    return await get_gumnut_client(jwt_token, library_id)


async def get_current_library_id(
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> str | None:
    """The library bound for this request, for the Gumnut calls that take
    ``library_id`` in a body or form. Depending on the (per-request cached)
    client guarantees the library is resolved first.
    """
    return get_bound_library_id()


async def get_authenticated_gumnut_client_optional(
    request: Request,
) -> AsyncGumnut | None:
    """
    Dependency that provides an authenticated AsyncGumnut client for the current request
    if a JWT is present. Otherwise, returns None without raising an exception.

    Used during logout to prevent errors when no JWT is present. The client is
    not library-scoped: logout has no library to act on.

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
