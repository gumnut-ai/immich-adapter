"""Choose and cache the Gumnut library an Immich client acts on.

See docs/architecture/adapter-architecture.md § Library scope.
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

# API keys have no session to expire with, so their cached library is bounded
# by a TTL instead; library-not-found drops the entry earlier.
API_KEY_LIBRARY_TTL_SECONDS = 60 * 60


def first_live_library_id(libraries: Iterable[LibraryResponse]) -> str | None:
    """The oldest live library, or ``None`` when the user has none.

    The Gumnut API lists libraries newest first, so sort rather than trust it.
    """
    ordered = sorted(libraries, key=lambda library: (library.created_at, library.id))
    return ordered[0].id if ordered else None


def is_library_not_found(detail: str, library_id: str) -> bool:
    """Whether a Gumnut 404 detail says the bound library is gone.

    The Gumnut API rejects a trashed, purged, or unowned library with
    ``Library <id> not found or not accessible by user <user>`` (the id is
    sometimes quoted, the user suffix sometimes absent). Requiring the bound
    id keeps another entity's 404 from being read as a vanished library.
    """
    return detail.startswith("Library ") and library_id in detail


def _api_key_cache_key(api_key: str) -> str:
    # Only a digest of the key reaches Redis; the key itself is a credential.
    return f"library:apikey:{hashlib.sha256(api_key.encode()).hexdigest()}"


class LibraryCache:
    """Per-credential cache of the resolved library id.

    Best-effort: a Redis failure is logged and treated as a miss or a no-op
    write, so the request resolves from the Gumnut API rather than fail.
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
