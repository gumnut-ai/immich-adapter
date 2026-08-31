"""Unauthenticated public configuration endpoints (Immich v3.2+).

The v3.2 web login page calls ``getPublicConfig`` before any authentication,
so these routes are also listed in ``AuthMiddleware.UNAUTHENTICATED_PATHS`` —
implementing the route without that would still break login with a 401.

The payload re-projects the same fixed values the adapter serves through the
deprecated ``/server/config`` and ``/server/features`` endpoints in
``routers/api/server.py``: OAuth is the only login method ("Sign in with
Gumnut", auto-launched), password login is disabled, and there is no login
page message or custom CSS.
"""

from fastapi import APIRouter

from routers.immich_models import (
    PublicConfigDto,
    PublicConfigOAuthDto,
    PublicConfigPasswordLoginDto,
    PublicConfigServerDto,
    PublicConfigThemeDto,
)

router = APIRouter(
    prefix="/api/public",
    tags=["public"],
    responses={404: {"description": "Not found"}},
)


def _public_config() -> PublicConfigDto:
    return PublicConfigDto(
        oauth=PublicConfigOAuthDto(
            enabled=True,
            autoLaunch=True,
            # Shown as the OAuth button label if the login form is visible
            # (e.g., ?autoLaunch=0); mirrors /server/config's oauthButtonText.
            buttonText="Sign in with Gumnut",
        ),
        passwordLogin=PublicConfigPasswordLoginDto(enabled=False),
        server=PublicConfigServerDto(loginPageMessage=""),
        theme=PublicConfigThemeDto(customCss=""),
    )


@router.get("/config")
async def get_public_config() -> PublicConfigDto:
    """Get the configuration properties that are visible to everyone."""
    return _public_config()


@router.get("/config/defaults")
async def get_public_config_defaults() -> PublicConfigDto:
    """Get the default public configuration.

    The adapter's config is fixed rather than admin-editable, so its current
    values are also its defaults.
    """
    return _public_config()
