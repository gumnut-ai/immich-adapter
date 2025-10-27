"""HTTP client for communicating with Gumnut backend authentication endpoints."""

import httpx
import logging
from typing import Any

from pydantic import BaseModel

from config.settings import get_settings

logger = logging.getLogger(__name__)

_shared_backend_http_client: httpx.Client | None = None


def get_shared_backend_http_client() -> httpx.Client:
    """
    Get or create the shared HTTP client for backend authentication requests.

    This client is shared across all auth requests for connection pooling.

    Returns:
        httpx.Client: Shared HTTP client for backend communication
    """
    global _shared_backend_http_client

    if _shared_backend_http_client is None:
        _shared_backend_http_client = httpx.Client(
            timeout=30.0,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
            ),
        )

    return _shared_backend_http_client


def get_auth_url(
    redirect_uri: str,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
) -> dict[str, Any]:
    """
    Get OAuth authorization URL from backend.

    Calls backend GET /api/oauth/auth-url endpoint to get the OAuth provider
    authorization URL with CSRF state token.

    Args:
        redirect_uri: Callback URI for OAuth provider to redirect to
        code_challenge: Optional PKCE code challenge
        code_challenge_method: Optional PKCE code challenge method

    Returns:
        Dict containing 'url' key with authorization URL

    Raises:
        httpx.HTTPError: If backend request fails
    """
    settings = get_settings()
    client = get_shared_backend_http_client()

    # Build query parameters
    params = {"redirect_uri": redirect_uri}
    if code_challenge:
        params["code_challenge"] = code_challenge
    if code_challenge_method:
        params["code_challenge_method"] = code_challenge_method

    try:
        # Call backend
        response = client.get(
            f"{settings.gumnut_api_base_url}/api/oauth/auth-url",
            params=params,
        )
        response.raise_for_status()
        return response.json()

    except httpx.HTTPStatusError as e:
        logger.error(
            "Backend error getting auth URL",
            extra={"status_code": e.response.status_code},
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error("Failed to get auth URL", extra={"error": str(e)}, exc_info=True)
        raise


class ExchangeResults(BaseModel):
    access_token: str
    user_id: str
    email: str
    first_name: str | None
    last_name: str | None
    clerk_user_id: str
    is_active: bool
    is_verified: bool


def exchange_oauth_code(
    code: str,
    state: str,
    error: str | None = None,
    code_verifier: str | None = None,
) -> ExchangeResults:
    """
    Exchange OAuth authorization code for JWT token.

    Calls backend POST /api/oauth/exchange endpoint to exchange the OAuth
    authorization code for a JWT access token and user information.

    Args:
        code: OAuth authorization code from provider
        state: CSRF state token from OAuth flow
        error: Optional error from OAuth provider
        code_verifier: Optional PKCE code verifier

    Returns:
        Dict containing JWT and user information:
        - accessToken: JWT token string
        - userId: User UUID
        - userEmail: User email
        - name: User display name
        - isAdmin: Admin status
        - isOnboarded: Onboarding status
        - profileImagePath: Profile image URL
        - shouldChangePassword: Password change requirement

    Raises:
        httpx.HTTPError: If backend request fails
    """
    settings = get_settings()
    client = get_shared_backend_http_client()

    # Build request body
    body = {
        "code": code,
        "state": state,
    }
    if error:
        body["error"] = error
    if code_verifier:
        body["code_verifier"] = code_verifier

    try:
        # Call backend
        response = client.post(
            f"{settings.gumnut_api_base_url}/api/oauth/exchange",
            json=body,
        )
        response.raise_for_status()

        result = response.json()
        logger.info("OAuth token exchange successful")

        return ExchangeResults(
            access_token=result["access_token"],
            user_id=result["user"]["id"],
            email=result["user"]["email"],
            first_name=result["user"].get("first_name", None),
            last_name=result["user"].get("last_name", None),
            clerk_user_id=result["user"].get("clerk_user_id", ""),
            is_active=result["user"].get("is_active", False),
            is_verified=result["user"].get("is_verified", False),
        )

    except httpx.HTTPStatusError as e:
        logger.error(
            "Backend error exchanging token",
            extra={"status_code": e.response.status_code},
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error("Failed to exchange token", extra={"error": str(e)}, exc_info=True)
        raise
