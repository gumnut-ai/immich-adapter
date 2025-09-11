from fastapi import APIRouter

router = APIRouter(
    prefix="/immich/api/users",
    tags=["immich", "users"],
    responses={404: {"description": "Not found"}},
)


fake_users_me = {
    "id": "d6773835-4b91-4c7d-8667-26bd5daa1a45",
    "email": "ted@immich.test",
    "name": "Ted Mao",
    "profileImagePath": "",
    "avatarColor": "green",
    "profileChangedAt": "2025-01-13T21:43:39.959Z",
    "storageLabel": "admin",
    "shouldChangePassword": True,
    "isAdmin": True,
    "createdAt": "2025-01-13T21:43:39.959Z",
    "deletedAt": None,
    "updatedAt": "2025-01-24T20:14:54.325Z",
    "oauthId": "",
    "quotaSizeInBytes": None,
    "quotaUsageInBytes": 36821072368,
    "status": "active",
    "license": None,
}

fake_users_me_preferences = {
    "folders": {
        "enabled": False,
        "sidebarWeb": False,
    },
    "memories": {
        "enabled": True,
    },
    "people": {
        "enabled": True,
        "sidebarWeb": True,
    },
    "sharedLinks": {
        "enabled": False,
        "sidebarWeb": False,
    },
    "ratings": {
        "enabled": False,
    },
    "tags": {
        "enabled": False,
        "sidebarWeb": False,
    },
    "avatar": {"color": "green"},
    "emailNotifications": {
        "enabled": True,
        "albumInvite": True,
        "albumUpdate": True,
    },
    "download": {
        "archiveSize": 4294967296,
        "includeEmbeddedVideos": False,
    },
    "purchase": {
        "showSupportBadge": False,
        "hideBuyButtonUntil": "2027-02-12T00:00:00.000Z",
    },
}


@router.get("/me")
async def get_me():
    return fake_users_me


@router.get("/me/preferences")
async def get_me_preferences():
    return fake_users_me_preferences
