"""Choose and cache the Gumnut library an Immich client acts on.

The rule, the caching, and the invalidation are described in
docs/architecture/adapter-architecture.md § Library scope; the request-side
binding lives in ``routers/utils/gumnut_client.py``.
"""

import hashlib
import logging
from collections.abc import Iterable
from typing import Any

import redis.exceptions
from gumnut.types.library_response import LibraryResponse

from services.session_store import SessionStore
from utils.redis_client import get_redis_client
from utils.redis_protocols import AsyncRedisClient

logger = logging.getLogger(__name__)

# API keys are long-lived and have no session to expire with, so their cached
# library gets a bounded lifetime instead. Library-not-found drops the entry
# earlier; the TTL only caps how long a stale entry can survive a path that
# never reports the miss (e.g. a client that stops calling).
API_KEY_LIBRARY_TTL_SECONDS = 60 * 60


def first_live_library_id(libraries: Iterable[LibraryResponse]) -> str | None:
    """The oldest live library, or ``None`` when the user has none.

    The Gumnut API lists live libraries newest first, so the order is fixed
    here rather than trusting the response order.
    """
    ordered = sorted(libraries, key=lambda library: (library.created_at, library.id))
    return ordered[0].id if ordered else None


def is_library_not_found(detail: str, library_id: str) -> bool:
    """Whether a Gumnut 404 detail says the bound library is gone.

    Contract: the Gumnut API rejects an explicit library that is trashed,
    purged, or not owned with a detail of the form ``Library <id> not found
    or not accessible by user <user>`` (the id is sometimes quoted and the
    user suffix sometimes absent). Other 404s (asset, person, album) never
    name a library, and matching the bound id keeps a stale asset id from
    being mistaken for a vanished library.
    """
    return detail.startswith("Library ") and library_id in detail


def _api_key_cache_key(api_key: str) -> str:
    # Only a digest of the key reaches Redis; the key itself is a credential.
    return f"library:apikey:{hashlib.sha256(api_key.encode()).hexdigest()}"


class LibraryCache:
    """Per-credential cache of the resolved library id.

    Session-token clients cache on their session record; API-key clients,
    which carry no session, under a hashed-key entry with a TTL. Best-effort:
    a Redis failure is logged and treated as a miss or a no-op write, so the
    request still resolves the library from the Gumnut API rather than fail.
    """

    def __init__(self, redis_client: Any, session_store: SessionStore):
        # `Any` for the same reason as SessionStore: redis-py's union return
        # types don't satisfy the protocol under strict matching.
        self._redis: AsyncRedisClient = redis_client
        self._session_store = session_store

    async def get_for_api_key(self, api_key: str) -> str | None:
        try:
            value = await self._redis.get(_api_key_cache_key(api_key))
        except redis.exceptions.RedisError:
            logger.error("Failed to read cached library for API key", exc_info=True)
            return None
        return value or None

    async def remember_for_session(self, session_token: str, library_id: str) -> None:
        try:
            await self._session_store.update_library_id(session_token, library_id)
        except redis.exceptions.RedisError:
            logger.error("Failed to cache resolved library", exc_info=True)

    async def remember_for_api_key(self, api_key: str, library_id: str) -> None:
        try:
            await self._redis.set(
                _api_key_cache_key(api_key),
                library_id,
                ex=API_KEY_LIBRARY_TTL_SECONDS,
            )
        except redis.exceptions.RedisError:
            logger.error("Failed to cache resolved library", exc_info=True)

    async def forget_session(self, session_token: str) -> None:
        """Drop the cached library and queue a sync reset: the client's local
        copy and checkpoints belong to the library that just vanished."""
        try:
            await self._session_store.forget_library(session_token)
        except redis.exceptions.RedisError:
            logger.error("Failed to drop cached library", exc_info=True)

    async def forget_api_key(self, api_key: str) -> None:
        try:
            await self._redis.delete(_api_key_cache_key(api_key))
        except redis.exceptions.RedisError:
            logger.error("Failed to drop cached library", exc_info=True)


async def get_library_cache() -> LibraryCache:
    """FastAPI dependency providing a ``LibraryCache`` on the shared Redis client."""
    redis_client = await get_redis_client()
    return LibraryCache(redis_client, SessionStore(redis_client))
