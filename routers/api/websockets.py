import logging

import socketio

from config.settings import get_settings

logger = logging.getLogger(__name__)

# Create Socket.IO server
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


@sio.event
async def connect(sid, environ):
    logger.debug(f"Connect attempt - SID: {sid}")
    logger.debug(f"Environment: {environ}")
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
        logger.debug(f"Version info sent to {sid}")
    except Exception as e:
        logger.warn(f"Error in connect handler: {e}")


@sio.event
def connect_error(data):
    logger.warn(f"Connection error: {data}")


@sio.event
async def disconnect(sid):
    logger.debug(f"Client disconnected: {sid}")
