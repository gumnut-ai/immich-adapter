"""Utility for creating Gumnut client instances."""

import httpx
from gumnut import Gumnut

from config.settings import get_settings

_shared_http_client: httpx.Client | None = None


def get_shared_http_client() -> httpx.Client:
    """
    Get or create the shared HTTP client for Gumnut connections.

    This client is shared across all requests for connection pooling.
    Each Gumnut instance has its own JWT but shares the connection pool.

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
