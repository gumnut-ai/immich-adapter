"""Choose and cache the Gumnut library an Immich client acts on.

See docs/architecture/adapter-architecture.md § Library scope.
"""

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import redis.exceptions
from gumnut.types.library_response import LibraryResponse

from services.session_store import SessionStore
from utils.redis_client import get_redis_client
from utils.redis_protocols import AsyncRedisClient

logger = logging.getLogger(__name__)

# How long a resolved library is trusted before it is resolved again: a
# session's re-check interval and an API-key cache entry's TTL.
LIBRARY_RECHECK_SECONDS = 5 * 60

# Roles an Immich client can act in: it assumes it can upload and edit.
_CHOOSABLE_ROLES = frozenset({"owner", "collaborator"})


@dataclass(frozen=True)
class LibraryChoice:
    library_id: str
    from_choice: bool  # the user's stored choice, rather than the fallback


def first_owned_library_id(libraries: Iterable[LibraryResponse]) -> str | None:
    """The oldest live library the user owns, or ``None`` when they own none.

    The listing also carries libraries shared with the user, each with the
    caller's ``role``; joining an older one must not redirect uploads. The
    Gumnut API lists libraries newest first, so sort rather than trust it.
    """
    owned = [library for library in libraries if library.role == "owner"]
    ordered = sorted(owned, key=lambda library: (library.created_at, library.id))
    return ordered[0].id if ordered else None


def choose_library(
    libraries: Iterable[LibraryResponse],
    preferred_id: str | None,
    *,
    stay_on: str | None = None,
) -> LibraryChoice | None:
    """The library an Immich client acts on, or ``None`` when the user owns none
    and has no usable choice.

    ``libraries`` is the caller's live listing. The stored choice wins while it
    is listed with a role Immich can act in. Otherwise the fallback applies:
    ``stay_on`` (a library the session fell back to earlier) while the user
    still owns it, so restoring an older library does not move the session and
    force a resync; else the oldest owned library.
    """
    libraries = list(libraries)
    roles = {library.id: library.role for library in libraries}
    if preferred_id and roles.get(preferred_id) in _CHOOSABLE_ROLES:
        return LibraryChoice(preferred_id, from_choice=True)
    if stay_on and roles.get(stay_on) == "owner":
        return LibraryChoice(stay_on, from_choice=False)
    fallback = first_owned_library_id(libraries)
    return LibraryChoice(fallback, from_choice=False) if fallback else None


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

    async def remember_for_session(
        self, session_token: str, previous_library_id: str, choice: LibraryChoice
    ) -> None:
        """Record a resolution that did not change the session's library;
        ``previous_library_id`` is ``""`` for a session's first resolution."""
        try:
            await self._session_store.update_library_id(
                session_token,
                previous_library_id,
                choice.library_id,
                from_choice=choice.from_choice,
            )
        except redis.exceptions.RedisError:
            logger.error("Failed to cache resolved library", exc_info=True)

    async def switch_session(
        self, session_token: str, previous_library_id: str, choice: LibraryChoice
    ) -> None:
        """Move the session to another library and queue a sync reset."""
        try:
            await self._session_store.switch_library(
                session_token,
                previous_library_id,
                choice.library_id,
                from_choice=choice.from_choice,
            )
        except redis.exceptions.RedisError:
            logger.error("Failed to switch cached library", exc_info=True)

    async def remember_for_api_key(self, api_key: str, library_id: str) -> None:
        try:
            await self._redis.set(
                _api_key_cache_key(api_key),
                library_id,
                ex=LIBRARY_RECHECK_SECONDS,
            )
        except redis.exceptions.RedisError:
            logger.error("Failed to cache resolved library", exc_info=True)

    async def forget_session(
        self, session_token: str, previous_library_id: str
    ) -> None:
        """Drop the cached library and queue a sync reset: the client's local
        copy and checkpoints belong to a library it can no longer use."""
        try:
            await self._session_store.forget_library(session_token, previous_library_id)
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
