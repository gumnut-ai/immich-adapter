from uuid import UUID
import logging

from fastapi import APIRouter, Depends, Request, Response
from gumnut import Gumnut, GumnutError

from routers.immich_models import (
    AuthStatusResponseDto,
    ChangePasswordDto,
    LoginCredentialDto,
    LoginResponseDto,
    LogoutResponseDto,
    PinCodeChangeDto,
    PinCodeResetDto,
    PinCodeSetupDto,
    SessionUnlockDto,
    SignUpDto,
    UserAdminResponseDto,
    ValidateAccessTokenResponseDto,
)
from routers.utils.cookies import AuthType, ImmichCookie, set_auth_cookies
from routers.utils.gumnut_client import (
    get_authenticated_gumnut_client_optional,
)
from socketio.exceptions import SocketIOError

from services.websockets import emit_event, WebSocketEvent
from services.session_store import SessionStore, get_session_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/auth",
    tags=["auth"],
    responses={404: {"description": "Not found"}},
)


fake_auth_login = {
    "accessToken": "y3NP8DRmNE1K2DCNsVZKPepmqIWXQyoghTGS9aDjBM",
    "userId": UUID("d6773835-4b91-4c7d-8667-26bd5daa1a45"),
    "userEmail": "ted@immich.test",
    "name": "Ted Mao",
    "isAdmin": True,
    "isOnboarded": True,
    "profileImagePath": "",
    "shouldChangePassword": False,
}


@router.post("/admin-sign-up", status_code=201, response_model=UserAdminResponseDto)
async def sign_up_admin(request: SignUpDto):
    """
    Admin sign up endpoint.
    This is a stub implementation that returns a fake login response.
    """
    return


@router.post("/change-password", status_code=200, response_model=UserAdminResponseDto)
async def change_password(request: ChangePasswordDto):
    """
    Change user password.
    This is a stub implementation that returns a fake login response.
    """
    return


@router.post("/login", status_code=201)
async def post_login(
    body: LoginCredentialDto, request: Request, response: Response
) -> LoginResponseDto:
    set_auth_cookies(
        response,
        fake_auth_login["accessToken"],
        AuthType.PASSWORD,
        request.url.scheme == "https",
    )

    return LoginResponseDto(
        accessToken=fake_auth_login["accessToken"],
        isAdmin=fake_auth_login["isAdmin"],
        isOnboarded=fake_auth_login["isOnboarded"],
        name=fake_auth_login["name"],
        profileImagePath=fake_auth_login["profileImagePath"],
        shouldChangePassword=fake_auth_login["shouldChangePassword"],
        userEmail=body.email or fake_auth_login["userEmail"],
        userId=str(fake_auth_login["userId"]),
    )


@router.post("/logout")
async def post_logout(
    request: Request,
    response: Response,
    client: Gumnut | None = Depends(get_authenticated_gumnut_client_optional),
    session_store: SessionStore = Depends(get_session_store),
) -> LogoutResponseDto:
    auth_type = request.cookies.get(ImmichCookie.AUTH_TYPE.value)

    # Delete session from SessionStore before clearing cookies
    # Use session_token from request.state (set by auth middleware), fall back to cookie
    session_token = getattr(request.state, "session_token", None)
    if not session_token:
        session_token = request.cookies.get(ImmichCookie.ACCESS_TOKEN.value)

    if session_token:
        try:
            await session_store.delete(session_token)
            # Emit WebSocket event to notify the session's client
            try:
                await emit_event(
                    WebSocketEvent.SESSION_DELETE, session_token, session_token
                )
            except SocketIOError as ws_error:
                logger.warning(
                    "Failed to emit WebSocket event after logout",
                    extra={"session_id": session_token, "error": str(ws_error)},
                )
        except Exception as e:
            # Log but don't fail the logout - cookie clearing is more important
            logger.warning(
                "Failed to delete session on logout",
                extra={"error": str(e)},
                exc_info=True,
            )

    response.delete_cookie(ImmichCookie.ACCESS_TOKEN.value)
    response.delete_cookie(ImmichCookie.AUTH_TYPE.value)
    response.delete_cookie(ImmichCookie.IS_AUTHENTICATED.value)

    # By setting autoLaunch=0, we prevent the Immich web client from immediately launching
    # the login flow again after logout.
    redirect_uri = "/auth/login?autoLaunch=0"
    if auth_type == AuthType.OAUTH.value and client is not None:
        try:
            logout_response = client.oauth.logout_endpoint()
            redirect_uri = logout_response.logout_endpoint
        except GumnutError:
            logger.warning(
                "OAuth provider does not support logout endpoint", exc_info=True
            )
        except Exception:
            logger.error(
                "Unexpected error while calling OAuth logout endpoint", exc_info=True
            )

    return LogoutResponseDto(
        redirectUri=redirect_uri,
        successful=True,
    )


@router.post("/pin-code", status_code=204)
async def setup_pin_code(request: PinCodeSetupDto):
    """
    Setup PIN code for the user.
    This is a stub implementation that does nothing.
    """
    return


@router.put("/pin-code", status_code=204)
async def change_pin_code(request: PinCodeChangeDto):
    """
    Change PIN code for the user.
    This is a stub implementation that does nothing.
    """
    return


@router.delete("/pin-code", status_code=204)
async def reset_pin_code(request: PinCodeResetDto):
    """
    Reset PIN code for the user.
    This is a stub implementation that does nothing.
    """
    return


@router.post("/session/lock", status_code=204)
async def lock_auth_session():
    """
    Lock the current session.
    This is a stub implementation that does nothing.
    """
    return


@router.post("/session/unlock", status_code=204)
async def unlock_auth_session(request: SessionUnlockDto):
    """
    Unlock the current session.
    This is a stub implementation that returns a fake login response.
    """
    return


@router.get("/status")
async def get_auth_status() -> AuthStatusResponseDto:
    """
    Check the authentication status of the user.
    This is a stub implementation that returns basic auth status.
    """
    return AuthStatusResponseDto(
        expiresAt=None, isElevated=False, password=True, pinCode=False
    )


@router.post("/validateToken")
async def validate_access_token() -> ValidateAccessTokenResponseDto:
    """
    Validate access token.
    This is a stub implementation that always returns valid auth status.
    """
    return ValidateAccessTokenResponseDto(authStatus=True)
