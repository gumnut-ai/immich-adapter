from fastapi import FastAPI
from config.sentry import init_sentry
from config.logging import init_logging
from contextlib import asynccontextmanager

from routers.middleware.auth_middleware import AuthMiddleware
from routers import static, well_known
from routers.utils.spa_static_files import SPAStaticFiles
from routers.api import (
    activities,
    admin,
    albums,
    api_keys,
    assets,
    auth,
    download,
    duplicates,
    faces,
    jobs,
    libraries,
    map,
    memories,
    oauth,
    notifications,
    partners,
    people,
    search,
    server,
    sessions,
    shared_links,
    stacks,
    sync,
    system_config,
    system_metadata,
    tags,
    timeline,
    trash,
    users,
    view,
    websockets,
)
from routers.utils.gumnut_client import close_shared_http_client
from utils.redis_client import close_redis_client

init_logging()
init_sentry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager to handle startup and shutdown tasks.
    Code before yield runs on startup, code after yield runs on shutdown.
    """
    yield
    # Ensure the singleton HTTP client for Gumnut is closed on shutdown
    await close_shared_http_client()
    # Close Redis client
    await close_redis_client()


app = FastAPI(
    title="Immich Adapter for Gumnut",
    version="0.1.0",
    description="Adapts the Immich API to the Gumnut API",
    lifespan=lifespan,
)

# Add authentication middleware
app.add_middleware(AuthMiddleware)

# Mount Socket.IO app first
app.mount("/api/socket.io", websockets.socket_app)

# Then include other routers
app.include_router(activities.router)
app.include_router(admin.router)
app.include_router(albums.router)
app.include_router(api_keys.router)
app.include_router(assets.router)
app.include_router(auth.router)
app.include_router(download.router)
app.include_router(duplicates.router)
app.include_router(faces.router)
app.include_router(jobs.router)
app.include_router(libraries.router)
app.include_router(map.router)
app.include_router(memories.router)
app.include_router(oauth.router)
app.include_router(notifications.router)
app.include_router(partners.router)
app.include_router(people.router)
app.include_router(search.router)
app.include_router(server.router)
app.include_router(sessions.router)
app.include_router(shared_links.router)
app.include_router(static.router)
app.include_router(well_known.router)
app.include_router(stacks.router)
app.include_router(sync.router)
app.include_router(system_config.router)
app.include_router(system_metadata.router)
app.include_router(tags.router)
app.include_router(timeline.router)
app.include_router(trash.router)
app.include_router(users.router)
app.include_router(view.router)

# Mount static files at root - must be last to avoid conflicts with API routes
# Use SPAStaticFiles to serve index.html for SPA routes (e.g., /auth/login)
app.mount("/", SPAStaticFiles(directory="static", html=True), name="staticFileHosting")
