from fastapi import Response
from enum import Enum

# 400-day Max-Age on auth cookies. Without an explicit Max-Age these become
# session cookies, which iOS HTTPCookieStorage holds in memory only and drops
# on app process death — the upstream Immich server and iOS client both encode
# a 400-day cookie lifetime, so the adapter must match to avoid forcing
# re-login every time iOS reaps the backgrounded app.
COOKIE_MAX_AGE_SECONDS = 400 * 24 * 60 * 60


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
        - HttpOnly: True for ACCESS_TOKEN and AUTH_TYPE; IS_AUTHENTICATED is
          intentionally JS-readable so the frontend can branch on it
        - Secure: True by default, or based on secure parameter
        - SameSite: "lax" (CSRF protection while allowing some cross-site navigation)
        - Max-Age: COOKIE_MAX_AGE_SECONDS (persists across iOS app process death)
    """
    # Set access token cookie (most sensitive, always HttpOnly)
    response.set_cookie(
        key=ImmichCookie.ACCESS_TOKEN.value,
        value=access_token,
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
    )

    # Set auth type cookie (indicates how user authenticated)
    response.set_cookie(
        key=ImmichCookie.AUTH_TYPE.value,
        value=auth_type.value,
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
    )

    # Set is_authenticated cookie (convenience flag for frontend)
    response.set_cookie(
        key=ImmichCookie.IS_AUTHENTICATED.value,
        value="true",
        max_age=COOKIE_MAX_AGE_SECONDS,
        secure=secure,
        samesite="lax",
    )
