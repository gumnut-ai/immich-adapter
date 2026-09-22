import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from functools import partial

import httpx
from contextvars import ContextVar
from dataclasses import dataclass
from fastapi import Depends, HTTPException, Request, status
from gumnut import AsyncGumnut, PermissionDeniedError

from config.settings import get_settings
from services.library_resolver import (
    LIBRARY_RECHECK_SECONDS,
    LibraryCache,
    LibraryChoice,
    choose_library,
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


async def _fetch_library_choice(
    request: Request, credential: str, stay_on: str | None
) -> LibraryChoice | None:
    """Resolve the library from the Gumnut API: the user's stored choice when
    usable, else the fallback (see ``choose_library``).

    ``None`` leaves the request unscoped: the credential cannot list libraries
    (an API key limited to selected libraries, whose preference is therefore
    not consulted), or the user has no live library. A user with only shared
    libraries and no usable choice is refused with a 403.
    """
    unscoped = await get_gumnut_client(credential)
    try:
        libraries = await unscoped.libraries.list()
    except PermissionDeniedError:
        logger.warning(
            "Credential cannot list libraries; leaving calls unscoped",
            extra={"path": request.url.path},
        )
        return None
    if not libraries:
        logger.info("User has no live library; leaving calls unscoped")
        return None

    try:
        preferred_id = (await unscoped.users.me()).immich_library_id
    except PermissionDeniedError:
        preferred_id = None
    choice = choose_library(libraries, preferred_id, stay_on=stay_on)
    if choice is None:
        # Unscoped, the Gumnut API would default to a lone shared library and
        # take this client's uploads into it.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Immich needs a Gumnut library: choose one in the Immich section "
                "of your Gumnut web settings, or create one of your own"
            ),
        )
    return choice


async def _resolve_library_id(
    request: Request, credential: str, cache: LibraryCache
) -> str | None:
    """Resolve the library for this request's credential, caching the result.

    ``credential`` is the session's JWT or the raw API key. A cached library is
    trusted for ``LIBRARY_RECHECK_SECONDS``, then resolved again; a session
    whose library changes is flagged for a sync reset. ``None`` leaves the
    request unscoped and caches nothing.
    """
    session_token = getattr(request.state, "session_token", None)
    if session_token:
        cached = getattr(request.state, "session_library_id", None)
        checked_at = getattr(request.state, "session_library_checked_at", 0.0)
        from_choice = getattr(request.state, "session_library_from_choice", False)
        is_fresh = time.time() - checked_at < LIBRARY_RECHECK_SECONDS
        # Staying put applies only to a library the session fell back to.
        stay_on = None if from_choice else cached
    else:
        cached = await cache.get_for_api_key(credential)
        is_fresh = True  # the cache entry's TTL is the recheck bound
        stay_on = None

    if cached and is_fresh:
        library_id = cached
    else:
        try:
            choice = await _fetch_library_choice(request, credential, stay_on)
        except HTTPException:
            if cached and session_token:
                await cache.forget_session(session_token, cached)
            raise
        if choice is None:
            if cached and session_token:
                await cache.forget_session(session_token, cached)
            return None
        library_id = choice.library_id
        if not session_token:
            await cache.remember_for_api_key(credential, library_id)
        elif cached and cached != library_id:
            logger.info(
                "Session library changed; switching and resetting sync",
                extra={"previous_library_id": cached, "library_id": library_id},
            )
            await cache.switch_session(session_token, cached, choice)
        else:
            await cache.remember_for_session(session_token, cached or "", choice)

    if session_token:
        # Conditional on the bound library, so a request still bound to it
        # cannot drop a library another request already switched to.
        forget = partial(cache.forget_session, session_token, library_id)
    else:
        forget = partial(cache.forget_api_key, credential)
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
