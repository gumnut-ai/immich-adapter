from enum import Enum
from uuid import UUID

from fastapi import APIRouter, Response
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

router = APIRouter(
    prefix="/api/auth",
    tags=["auth"],
    responses={404: {"description": "Not found"}},
)


class ImmichCookie(str, Enum):
    ACCESS_TOKEN = "immich_access_token"
    AUTH_TYPE = "immich_auth_type"
    IS_AUTHENTICATED = "immich_is_authenticated"
    SHARED_LINK_TOKEN = "immich_shared_link_token"


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


def get_current_user_id() -> UUID:
    return fake_auth_login["userId"]


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
async def post_login(body: LoginCredentialDto, response: Response) -> LoginResponseDto:
    response.set_cookie(
        key=ImmichCookie.ACCESS_TOKEN.value,
        value=fake_auth_login["accessToken"],
        httponly=True,
        secure=True,  # Only send over HTTPS
        samesite="lax",  # CSRF protection (or "Strict" for more security)
    )
    response.set_cookie(
        key=ImmichCookie.AUTH_TYPE.value,
        value="password",
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
async def post_logout(response: Response) -> LogoutResponseDto:
    response.delete_cookie(ImmichCookie.ACCESS_TOKEN.value)
    response.delete_cookie(ImmichCookie.AUTH_TYPE.value)
    response.delete_cookie(ImmichCookie.IS_AUTHENTICATED.value)

    return LogoutResponseDto(
        redirectUri="/auth/login",
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
