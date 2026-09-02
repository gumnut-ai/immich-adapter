"""Public configuration used by the unauthenticated login page."""

from fastapi import APIRouter

from routers.api.constants import FIXED_LOGIN_CONFIG
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
    cfg = FIXED_LOGIN_CONFIG
    return PublicConfigDto(
        oauth=PublicConfigOAuthDto(
            enabled=cfg.oauth_enabled,
            autoLaunch=cfg.oauth_auto_launch,
            buttonText=cfg.oauth_button_text,
        ),
        passwordLogin=PublicConfigPasswordLoginDto(enabled=cfg.password_login_enabled),
        server=PublicConfigServerDto(loginPageMessage=cfg.login_page_message),
        theme=PublicConfigThemeDto(customCss=cfg.custom_css),
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
