from fastapi import Response
from enum import Enum


class ImmichCookie(str, Enum):
    ACCESS_TOKEN = "immich_access_token"
    AUTH_TYPE = "immich_auth_type"
    IS_AUTHENTICATED = "immich_is_authenticated"
    SHARED_LINK_TOKEN = "immich_shared_link_token"


class AuthType(str, Enum):
    OAUTH = "oauth"
    PASSWORD = "password"


def set_auth_cookies(
    response: Response,
    access_token: str,
    auth_type: AuthType,
    secure: bool = True,
) -> None:
    """
    Set authentication cookies with consistent security flags.

    This centralizes cookie-setting logic to ensure all auth endpoints use the same
    security settings (HttpOnly, Secure, SameSite) and avoids code duplication.

    Args:
        response: FastAPI Response object to set cookies on
        access_token: The JWT access token to store
        auth_type: Authentication type ("oauth", "password", etc.)
        secure: Optional boolean to determine if cookie should be secure.
                 If not provided, secure flag defaults to True.

    Security flags:
        - HttpOnly: True (prevents JavaScript access, XSS protection)
        - Secure: True by default, or based on secure parameter
        - SameSite: "lax" (CSRF protection while allowing some cross-site navigation)
    """
    # Set access token cookie (most sensitive, always HttpOnly)
    response.set_cookie(
        key=ImmichCookie.ACCESS_TOKEN.value,
        value=access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
    )

    # Set auth type cookie (indicates how user authenticated)
    response.set_cookie(
        key=ImmichCookie.AUTH_TYPE.value,
        value=auth_type.value,
        httponly=True,
        secure=secure,
        samesite="lax",
    )

    # Set is_authenticated cookie (convenience flag for frontend)
    response.set_cookie(
        key=ImmichCookie.IS_AUTHENTICATED.value,
        value="true",
        secure=secure,
        samesite="lax",
    )


def update_access_token_cookie(
    response: Response,
    access_token: str,
    secure: bool = True,
) -> None:
    """
    Update only the access token cookie (used for token refresh).

    This is used by middleware when refreshing tokens - we only need to update
    the access token, not the other auth cookies.

    Args:
        response: FastAPI Response object to set cookie on
        access_token: The new JWT access token
        secure: Optional boolean to determine if cookie should be secure.
                 If not provided, secure flag defaults to True.
    """
    response.set_cookie(
        key=ImmichCookie.ACCESS_TOKEN.value,
        value=access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
