from fastapi import FastAPI
from config.sentry import init_sentry
from config.logging import init_logging

from routers import static
from routers.api import albums
from routers.api import assets
from routers.api import (
    auth,
    partners,
    people,
    server,
    stacks,
    timeline,
    users,
    websockets,
    search,
)

init_logging()
init_sentry()

app = FastAPI(
    title="Immich Adapter for Gumnut",
    version="0.1.0",
    description="Adapts the Immich API to the Gumnut API",
)

# Mount Socket.IO app first
app.mount("/immich/api/socket.io", websockets.socket_app)

# Then include other routers
app.include_router(albums.router)
app.include_router(assets.router)
app.include_router(auth.router)
app.include_router(partners.router)
app.include_router(people.router)
app.include_router(server.router)
app.include_router(timeline.router)
app.include_router(users.router)
app.include_router(search.router)
app.include_router(static.router)
app.include_router(stacks.router)
