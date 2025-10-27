"""FastAPI dependencies for authentication and client creation."""

from fastapi import HTTPException, Request, status
from gumnut import Gumnut

from routers.utils.gumnut_client import get_gumnut_client
from config.settings import get_settings


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
