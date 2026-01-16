import logging
from enum import Enum
from http.cookies import SimpleCookie
from typing import Any, TypeAlias

import socketio
from pydantic import BaseModel

from config.settings import get_settings
from routers.immich_models import SyncAssetExifV1, SyncAssetV1
from services.session_store import (
    SessionDataError,
    SessionStoreError,
    get_session_store,
)

logger = logging.getLogger(__name__)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    engineio_version=4,
    logger=logging.getLogger("socketio"),  # type: ignore
    # Set to logging.getLogger("engineio") to see engineio logs
    engineio_logger=False,
)

# Create the ASGI app
socket_app = socketio.ASGIApp(socketio_server=sio, socketio_path="/")


class WebSocketEvent(Enum):
    """WebSocket events that can be emitted to clients."""

    # Phase 1: Can implement now
    UPLOAD_SUCCESS = "on_upload_success"
    ASSET_UPLOAD_READY_V1 = "AssetUploadReadyV1"
    ASSET_DELETE = "on_asset_delete"
    SESSION_DELETE = "on_session_delete"
    SERVER_VERSION = "on_server_version"

    # Phase 2: Requires photos-api event channel
    PERSON_THUMBNAIL = "on_person_thumbnail"


EventPayload: TypeAlias = BaseModel | str | list[str] | None


class AssetUploadReadyV1Payload(BaseModel):
    """Payload for the AssetUploadReadyV1 WebSocket event (mobile v2 sync)."""

    asset: SyncAssetV1
    exif: SyncAssetExifV1


# Maps socket ID -> (user_id, session_id) for disconnect cleanup
_sid_to_user: dict[str, tuple[str, str]] = {}


def _extract_session_token(environ: dict[str, Any]) -> str | None:
    """
    Extract session token from Socket.IO connection environment.

    Checks in order:
    1. x-immich-user-token header (mobile)
    2. Authorization: Bearer header (mobile alt)
    3. immich_access_token cookie (web)

    Args:
        environ: WSGI/ASGI environment dict from Socket.IO connect handler

    Returns:
        Session token string if found, None otherwise
    """
    # Check mobile header (HTTP_ prefix, dashes become underscores)
    if token := environ.get("HTTP_X_IMMICH_USER_TOKEN"):
        return token

    # Check Bearer token
    if auth := environ.get("HTTP_AUTHORIZATION", ""):
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            return token or None

    # Check cookie
    if cookie_str := environ.get("HTTP_COOKIE"):
        cookies = SimpleCookie()
        cookies.load(cookie_str)
        if "immich_access_token" in cookies:
            return cookies["immich_access_token"].value

    return None


@sio.event
async def connect(sid: str, environ: dict[str, Any]) -> bool | None:
    """
    Handle new WebSocket connection.

    Authenticates the client using session token, joins them to their
    user room, and sends initial server version info.

    Returns False to reject connections with invalid/missing tokens.
    """
    logger.debug("WebSocket connect attempt", extra={"sid": sid})

    # Extract session token
    session_token = _extract_session_token(environ)
    if not session_token:
        logger.warning(
            "WebSocket auth failed - no token found",
            extra={"sid": sid},
        )
        return False  # Reject connection

    # Look up session in Redis
    try:
        session_store = await get_session_store()
        session = await session_store.get_by_id(session_token)
    except (SessionStoreError, SessionDataError):
        logger.exception(
            "WebSocket auth failed - session lookup error",
            extra={"sid": sid, "session_token": session_token},
        )
        return False

    if not session:
        logger.warning(
            "WebSocket auth failed - session not found",
            extra={"sid": sid, "session_token": session_token},
        )
        return False  # Reject connection

    # Join user room and session room, and track session
    user_id = session.user_id
    session_id = str(session.id)
    await sio.enter_room(sid, user_id)
    await sio.enter_room(sid, session_id)
    _sid_to_user[sid] = (user_id, session_id)

    logger.debug(
        "WebSocket authenticated",
        extra={"sid": sid, "user_id": user_id},
    )

    # Send server version (existing behavior)
    version = get_settings().immich_version
    try:
        await sio.emit(
            "on_server_version",
            {
                "options": {},
                "loose": False,
                "includePrerelease": False,
                "raw": str(version),
                "major": version.major,
                "minor": version.minor,
                "patch": version.patch,
                "prerelease": [],
                "build": [],
                "version": str(version),
            },
            room=sid,
        )
        logger.debug("Version info sent", extra={"sid": sid})
    except Exception as e:
        logger.warning(
            "Error sending version info", extra={"sid": sid, "error": str(e)}
        )


@sio.event
def connect_error(data: Any) -> None:
    logger.warning("Connection error", extra={"data": data})


@sio.event
async def disconnect(sid: str) -> None:
    """Handle WebSocket disconnection."""
    ids = _sid_to_user.pop(sid, None)
    user_id, _ = ids if ids else (None, None)
    logger.debug(
        "WebSocket disconnected",
        extra={"sid": sid, "user_id": user_id},
    )


async def emit_event(
    event: WebSocketEvent,
    room: str,
    payload: EventPayload = None,
) -> None:
    """
    Emit a WebSocket event to a specific room.

    Args:
        event: The event type (from WebSocketEvent enum)
        room: The room to emit to (user_id for most events, session_id for SESSION_DELETE)
        payload: Event data - Pydantic model (auto-serialized), string, list, or None

    Raises:
        pydantic.ValidationError: If payload is a Pydantic model that fails serialization
        socketio.exceptions.SocketIOError: If the socket emission fails
    """
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    await sio.emit(event.value, data, room=room)
