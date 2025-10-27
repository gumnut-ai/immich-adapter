from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from gumnut import Gumnut, omit
from routers.immich_models import (
    LoginResponseDto,
    OAuthAuthorizeResponseDto,
    OAuthCallbackDto,
    OAuthConfigDto,
    UserAdminResponseDto,
    UserAvatarColor,
    UserLicense,
    UserStatus,
)
from routers.utils.dependencies import get_unauthenticated_gumnut_client
from routers.utils.oauth_utils import parse_callback_url
from routers.api.auth import ImmichCookie
from config.settings import get_settings

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/oauth",
    tags=["oauth"],
    responses={404: {"description": "Not found"}},
)


@router.post("/authorize", status_code=201)
async def start_oauth(
    oauth_config: OAuthConfigDto,
    client: Gumnut = Depends(get_unauthenticated_gumnut_client),
) -> OAuthAuthorizeResponseDto:
    """
    Start OAuth authentication process.

    Forwards the request to the Gumnut backend to get the OAuth provider
    authorization URL. The backend generates a CSRF state token and builds
    the complete authorization URL.

    Args:
        oauth_config: Configuration containing redirect URI and optional PKCE parameters

    Returns:
        OAuthAuthorizeResponseDto with the authorization URL

    Raises:
        HTTPException: If backend request fails
    """
    # Validate redirect URI against whitelist
    settings = get_settings()
    allowed_uris = settings.oauth_allowed_redirect_uris_list

    if oauth_config.redirectUri not in allowed_uris:
        logger.warning(
            "Invalid redirect_uri attempted",
            extra={
                "attempted_uri": oauth_config.redirectUri,
                "allowed_uris": list(allowed_uris),
            },
        )
        raise HTTPException(400, "Invalid redirect_uri")

    try:
        result = client.oauth.auth_url(
            redirect_uri=oauth_config.redirectUri,
            code_challenge=oauth_config.codeChallenge,
            code_challenge_method=oauth_config.codeChallenge,
            extra_headers={"Authorization": omit},  # Omit auth for this request
        )

        return OAuthAuthorizeResponseDto(url=result.url)

    except Exception as e:
        logger.error(
            "Failed to get OAuth authorization URL",
            extra={"error": str(e)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="OAuth authentication failed. Please try again.",
        )


@router.post("/callback", status_code=201)
async def finish_oauth(
    oauth_callback: OAuthCallbackDto,
    response: Response,
    client: Gumnut = Depends(get_unauthenticated_gumnut_client),
) -> LoginResponseDto:
    """
    Finish OAuth authentication process.

    Parses the OAuth callback URL to extract the authorization code and state,
    forwards them to the backend for token exchange, receives the JWT and user
    info, sets authentication cookies, and returns the login response.

    Args:
        oauth_callback: Contains the callback URL with OAuth response parameters
        response: FastAPI response object for setting cookies

    Returns:
        LoginResponseDto with JWT and user information

    Raises:
        HTTPException: If URL parsing fails or backend request fails
    """
    try:
        # Parse callback URL to extract code, state, and error
        parsed = parse_callback_url(oauth_callback.url)

        logger.info(
            "OAuth callback received",
            extra={
                "has_error": parsed["error"] is not None,
                "error": parsed["error"],
            },
        )

        # Exchange authorization code for JWT
        result = client.oauth.exchange(
            code=parsed["code"],
            state=parsed["state"],
            error=parsed["error"],
            code_verifier=oauth_callback.codeVerifier,
            extra_headers={"Authorization": omit},  # Omit auth for this request
        )

        # Set authentication cookies for web client
        response.set_cookie(
            key=ImmichCookie.ACCESS_TOKEN.value,
            value=result.access_token,
            httponly=True,
            secure=True,  # Only send over HTTPS
            samesite="lax",  # CSRF protection (or "Strict" for more security)
        )
        response.set_cookie(
            key=ImmichCookie.AUTH_TYPE.value,
            value="oauth",
            httponly=True,
            secure=True,
            samesite="lax",
        )
        response.set_cookie(
            key=ImmichCookie.IS_AUTHENTICATED.value,
            value="true",
            secure=True,
            samesite="lax",
        )

        # Return login response with JWT and user info
        return LoginResponseDto(
            accessToken=result.access_token,
            userId=result.user.id,
            userEmail=result.user.email or "",
            name=f"{result.user.first_name or ''} {result.user.last_name or ''}".strip()
            if result.user.first_name or result.user.last_name
            else result.user.email or "",
            isAdmin=False,  # TODO: determine admin status
            isOnboarded=True,  # TODO: determine onboarding status
            profileImagePath="",
            shouldChangePassword=False,
        )

    except ValueError as e:
        # URL parsing error
        logger.error("Failed to parse OAuth callback URL", extra={"error": str(e)})
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Backend communication error
        logger.error(
            "Failed to complete OAuth authentication",
            extra={"error": str(e)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="OAuth authentication failed. Please try again.",
        )


@router.post("/link")
async def link_oauth_account(oauth_callback: OAuthCallbackDto) -> UserAdminResponseDto:
    """
    Link OAuth account to existing user
    This is a stub implementation that returns a fake user response.
    """
    now = datetime.now(timezone.utc)
    return UserAdminResponseDto(
        avatarColor=UserAvatarColor.primary,
        createdAt=now,
        deletedAt=now,
        email="ted@immich.test",
        id="d6773835-4b91-4c7d-8667-26bd5daa1a45",
        isAdmin=True,
        license=UserLicense(
            activatedAt=now,
            activationKey="dummy-activation-key",
            licenseKey="dummy-license-key",
        ),
        name="Ted Mao",
        oauthId="oauth-123456",
        profileChangedAt=now,
        profileImagePath="",
        quotaSizeInBytes=0,
        quotaUsageInBytes=0,
        shouldChangePassword=False,
        status=UserStatus.active,
        storageLabel="default",
        updatedAt=now,
    )


@router.get("/mobile-redirect")
async def redirect_oauth_to_mobile():
    """
    Redirect OAuth to mobile application
    This is a stub implementation that does not perform any action.
    """
    return {"message": "Redirecting to mobile app"}


@router.post("/unlink")
async def unlink_oauth_account() -> UserAdminResponseDto:
    """
    Unlink OAuth account from user
    This is a stub implementation that returns a fake user response.
    """
    now = datetime.now(timezone.utc)
    return UserAdminResponseDto(
        avatarColor=UserAvatarColor.primary,
        createdAt=now,
        deletedAt=now,
        email="ted@immich.test",
        id="d6773835-4b91-4c7d-8667-26bd5daa1a45",
        isAdmin=True,
        license=UserLicense(
            activatedAt=now,
            activationKey="dummy-activation-key",
            licenseKey="dummy-license-key",
        ),
        name="Ted Mao",
        oauthId="",
        profileChangedAt=now,
        profileImagePath="",
        quotaSizeInBytes=0,
        quotaUsageInBytes=0,
        shouldChangePassword=False,
        status=UserStatus.active,
        storageLabel="default",
        updatedAt=now,
    )
