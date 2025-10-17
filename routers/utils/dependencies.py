"""FastAPI dependencies for authentication and client creation."""

from fastapi import HTTPException, Request, status
from gumnut import Gumnut

from routers.utils.gumnut_client import get_gumnut_client


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
