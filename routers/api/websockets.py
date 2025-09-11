import logging

import socketio

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
socket_app = socketio.ASGIApp(socketio_server=sio, socketio_path="")


@sio.event
async def connect(sid, environ):
    logger.debug(f"Connect attempt - SID: {sid}")
    logger.debug(f"Environment: {environ}")
    try:
        await sio.emit(
            "on_server_version",
            {
                "options": {},
                "loose": False,
                "includePrerelease": False,
                "raw": "1.125.3",
                "major": 1,
                "minor": 125,
                "patch": 3,
                "prerelease": [],
                "build": [],
                "version": "1.125.3",
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
