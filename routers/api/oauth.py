from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
from routers.utils.gumnut_client import get_unauthenticated_gumnut_client
from routers.utils.oauth_utils import parse_callback_url
from routers.utils.cookies import set_auth_cookies
from config.settings import get_settings

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/oauth",
    tags=["oauth"],
    responses={404: {"description": "Not found"}},
)


def rewrite_redirect_uri(uri: str, request: Request) -> str:
    """
    Rewrite redirect URI to use custom scheme for mobile apps.

    Some OAuth providers don't support custom URL schemes (app.immich:///).
    This function rewrites the mobile redirect URI to use a standard HTTPS URL
    that the adapter hosts, which then redirects back to the mobile app.

    Handles reverse proxy scenarios (like Render) by checking X-Forwarded-* headers
    to build the correct public URL. Uses the request's URL if not behind a proxy.

    Args:
        uri: Original redirect URI
        request: FastAPI request object

    Returns:
        Rewritten redirect URI for mobile apps
    """
    settings = get_settings()
    mobile_scheme = settings.oauth_mobile_redirect_uri

    if uri == mobile_scheme:
        # Check for reverse proxy headers (e.g., from Render, nginx, etc.)
        forwarded_proto = request.headers.get("x-forwarded-proto")
        forwarded_host = request.headers.get("x-forwarded-host")

        if forwarded_proto and forwarded_host:
            # Behind a reverse proxy - use forwarded headers to build public URL
            return str(
                request.url.replace(
                    scheme=forwarded_proto,
                    netloc=forwarded_host,
                    path="/api/oauth/mobile-redirect",
                )
            )
        else:
            # Build an absolute URL based on the current request's scheme/host
            return str(request.url_for("redirect_oauth_to_mobile"))
    return uri


@router.post("/authorize", status_code=201)
async def start_oauth(
    oauth_config: OAuthConfigDto,
    request: Request,
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
    try:
        redirectUri = rewrite_redirect_uri(oauth_config.redirectUri, request)
        result = client.oauth.auth_url(
            redirect_uri=redirectUri,
            code_challenge=oauth_config.codeChallenge,
            code_challenge_method="S256" if oauth_config.codeChallenge else None,
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
    request: Request,
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
    except ValueError as e:
        logger.error("Failed to parse OAuth callback URL", extra={"error": str(e)})
        raise HTTPException(
            status_code=400, detail="OAuth authentication failed. Please try again."
        )

    try:
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
        set_auth_cookies(
            response, result.access_token, "oauth", request.url.scheme == "https"
        )

        if result.user.first_name or result.user.last_name:
            name = (
                f"{result.user.first_name or ''} {result.user.last_name or ''}".strip()
            )
        else:
            name = result.user.email or ""

        # Return login response with JWT and user info
        return LoginResponseDto(
            accessToken=result.access_token,
            userId=result.user.id,
            userEmail=result.user.email or "",
            name=name,
            isAdmin=False,  # TODO: determine admin status
            isOnboarded=True,  # TODO: determine onboarding status
            profileImagePath="",
            shouldChangePassword=False,
        )

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
async def redirect_oauth_to_mobile(request: Request):
    """
    Redirect OAuth to mobile application.

    This endpoint is used when an OAuth provider cannot support custom URL schemes.
    It receives the OAuth response at a standard HTTPS URL and redirects to the
    mobile app using the configured mobile redirect URI (custom URL scheme).
    """
    from fastapi.responses import RedirectResponse

    settings = get_settings()

    # Get the query string from the request and append to mobile deep link
    query_string = request.url.query
    redirect_url = (
        f"{settings.oauth_mobile_redirect_uri}?{query_string}"
        if query_string
        else settings.oauth_mobile_redirect_uri
    )

    return RedirectResponse(url=redirect_url)


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
