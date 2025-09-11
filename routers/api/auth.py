from enum import Enum

from fastapi import APIRouter, Response

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
    "userId": "d6773835-4b91-4c7d-8667-26bd5daa1a45",
    "userEmail": "ted@immich.test",
    "name": "Ted Mao",
    "isAdmin": True,
    "profileImagePath": "",
    "shouldChangePassword": False,
}


@router.post("/login")
async def post_login(response: Response):
    response.set_cookie(
        key=ImmichCookie.ACCESS_TOKEN.value,
        value=fake_auth_login["accessToken"],
        httponly=True,
    )
    response.set_cookie(
        key=ImmichCookie.AUTH_TYPE.value,
        value="password",
        httponly=True,
    )
    response.set_cookie(
        key=ImmichCookie.IS_AUTHENTICATED.value,
        value="true",
    )
    return fake_auth_login
