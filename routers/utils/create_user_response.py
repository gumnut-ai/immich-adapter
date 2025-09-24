from datetime import datetime, timezone
from routers.api.auth import get_current_user_id
from routers.immich_models import UserAvatarColor, UserResponseDto


def create_user_response_dto() -> UserResponseDto:
    """
    As there is just one user in the system for now, return a static user response.
    """
    return UserResponseDto(
        id=str(get_current_user_id()),
        email="",
        avatarColor=UserAvatarColor.primary,
        name="",
        profileImagePath="",
        profileChangedAt=datetime(1900, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )
