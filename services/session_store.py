import hashlib
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


class SessionDataError(Exception):
    """Raised when session data from Redis is invalid or corrupted."""

    pass


class SessionExpiredError(ValueError):
    """Raised when attempting to create a session with an expiration time in the past."""

    pass


# Protocol classes define the Redis interface used by SessionStore. This allows
# type checking without tight coupling to redis-py, and enables easy mocking in tests.
# redis-py uses union return types (e.g., Awaitable[T] | T) to support both sync and
# async clients, which makes strict Protocol matching difficult. We use permissive
# types (Any, Awaitable[Any]) to work around this.


class AsyncRedisPipeline(Protocol):
    """Protocol for async Redis pipeline operations."""

    def hset(
        self,
        name: str,
        key: str | None = None,
        value: str | None = None,
        mapping: dict[str, str] | None = None,
    ) -> Any: ...
    def hgetall(self, name: str) -> Any: ...
    def sadd(self, name: str, *values: str) -> Any: ...
    def zadd(self, name: str, mapping: dict[str, float]) -> Any: ...
    def expire(self, name: str, time: int) -> Any: ...
    def delete(self, *names: str) -> Any: ...
    def srem(self, name: str, *values: str) -> Any: ...
    def zrem(self, name: str, *values: str) -> Any: ...
    async def execute(self) -> list[Any]: ...


class AsyncRedisClient(Protocol):
    """Protocol for async Redis client operations used by SessionStore."""

    def pipeline(self) -> AsyncRedisPipeline: ...
    def hgetall(self, name: str) -> Awaitable[dict[Any, Any]]: ...
    def hset(
        self,
        name: str,
        key: str | None = None,
        value: str | None = None,
        mapping: dict[str, str] | None = None,
    ) -> Awaitable[int]: ...
    def smembers(self, name: str) -> Awaitable[set[Any]]: ...
    def exists(self, *names: str) -> Awaitable[int]: ...
    def ttl(self, name: str) -> Awaitable[int]: ...
    def zrangebyscore(
        self, name: str, min: float, max: float
    ) -> Awaitable[list[Any]]: ...


_REQUIRED_SESSION_FIELDS = frozenset(
    [
        "user_id",
        "library_id",
        "device_type",
        "device_os",
        "app_version",
        "created_at",
        "updated_at",
        "is_pending_sync_reset",
    ]
)


@dataclass
class Session:
    """Session data."""

    id: str
    user_id: str
    library_id: str
    device_type: str
    device_os: str
    app_version: str
    created_at: datetime
    updated_at: datetime
    is_pending_sync_reset: bool

    def to_dict(self) -> dict[str, str]:
        """Convert to Redis hash format."""
        return {
            "user_id": self.user_id,
            "library_id": self.library_id,
            "device_type": self.device_type,
            "device_os": self.device_os,
            "app_version": self.app_version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "is_pending_sync_reset": "1" if self.is_pending_sync_reset else "0",
        }

    @classmethod
    def from_dict(cls, session_id: str, data: dict[str, str]) -> "Session":
        """
        Create from Redis hash data.

        Args:
            session_id: The hashed session ID
            data: Redis hash data containing session fields

        Returns:
            Session object

        Raises:
            SessionDataError: If required fields are missing or data is malformed
        """
        missing_fields = _REQUIRED_SESSION_FIELDS - set(data.keys())
        if missing_fields:
            raise SessionDataError(
                f"Session {session_id} is missing required fields: {missing_fields}"
            )

        try:
            return cls(
                id=session_id,
                user_id=data["user_id"],
                library_id=data["library_id"],
                device_type=data["device_type"],
                device_os=data["device_os"],
                app_version=data["app_version"],
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                is_pending_sync_reset=data["is_pending_sync_reset"] == "1",
            )
        except ValueError as e:
            raise SessionDataError(
                f"Session {session_id} has malformed data: {e}"
            ) from e


class SessionStore:
    """
    Abstraction layer for session storage.

    Hides Redis implementation details from calling code.
    All session operations go through this class.
    """

    def __init__(self, redis_client: Any):
        """
        Initialize SessionStore with a Redis client.

        Args:
            redis_client: An async Redis client (redis.asyncio.Redis)
        """
        self._redis: AsyncRedisClient = redis_client

    @staticmethod
    def hash_jwt(jwt_token: str) -> str:
        """Hash JWT to create session ID."""
        return hashlib.sha256(jwt_token.encode()).hexdigest()

    async def create(
        self,
        jwt_token: str,
        user_id: str,
        library_id: str,
        device_type: str,
        device_os: str,
        app_version: str,
        expires_at: datetime | None = None,
    ) -> Session:
        """
        Create a new session.

        Args:
            jwt_token: The Gumnut JWT (will be hashed to create session ID)
            user_id: Gumnut user ID from JWT claims
            library_id: User's default library ID
            device_type: "iOS", "Android", "Chrome", etc.
            device_os: "iOS 17.4", "Android 13", etc.
            app_version: "1.94.0" or empty string for web
            expires_at: Optional expiration time (uses Redis TTL)

        Returns:
            The created Session object

        Raises:
            SessionExpiredError: If expires_at is in the past
        """
        session_id = self.hash_jwt(jwt_token)
        now = datetime.now(timezone.utc)

        if expires_at is not None and expires_at <= now:
            raise SessionExpiredError(
                f"Cannot create session with expiration time in the past: {expires_at}"
            )

        session = Session(
            id=session_id,
            user_id=user_id,
            library_id=library_id,
            device_type=device_type,
            device_os=device_os,
            app_version=app_version,
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )

        pipe = self._redis.pipeline()
        pipe.hset(f"session:{session_id}", mapping=session.to_dict())
        pipe.sadd(f"user:{user_id}:sessions", session_id)
        pipe.zadd("sessions:by_updated_at", {session_id: now.timestamp()})

        if expires_at is not None:
            ttl_seconds = int((expires_at - now).total_seconds())
            if ttl_seconds <= 0:
                # Should have been caught by the earlier `expires_at <= now` check,
                # but guard against rounding issues.
                ttl_seconds = 1
            pipe.expire(f"session:{session_id}", ttl_seconds)

        await pipe.execute()
        return session

    async def get(self, jwt_token: str) -> Session | None:
        """
        Get session by JWT token.

        Args:
            jwt_token: The Gumnut JWT

        Returns:
            Session if found, None otherwise
        """
        session_id = self.hash_jwt(jwt_token)
        return await self.get_by_id(session_id)

    async def get_by_id(self, session_id: str) -> Session | None:
        """
        Get session by session ID.

        Args:
            session_id: The hashed session ID

        Returns:
            Session if found, None otherwise
        """
        data = await self._redis.hgetall(f"session:{session_id}")
        if not data:
            return None
        return Session.from_dict(session_id, data)

    async def get_by_user(self, user_id: str) -> list[Session]:
        """
        Get all sessions for a user.

        Uses pipelining to fetch all sessions in a single round-trip.
        Automatically cleans up orphaned index entries for sessions
        that have expired via TTL.

        Args:
            user_id: Gumnut user ID

        Returns:
            List of Session objects
        """
        session_ids = await self._redis.smembers(f"user:{user_id}:sessions")
        if not session_ids:
            return []

        # Fetch all sessions in one pipeline
        session_id_list = list(session_ids)
        pipe = self._redis.pipeline()
        for session_id in session_id_list:
            pipe.hgetall(f"session:{session_id}")
        results = await pipe.execute()

        sessions = []
        orphaned_ids = []

        for session_id, data in zip(session_id_list, results):
            if data:
                try:
                    sessions.append(Session.from_dict(session_id, data))
                except SessionDataError:
                    # Treat corrupted sessions as orphaned
                    orphaned_ids.append(session_id)
            else:
                # Session expired via TTL, mark for cleanup
                orphaned_ids.append(session_id)

        # Clean up orphaned index entries
        if orphaned_ids:
            await self._cleanup_orphaned_indexes(user_id, orphaned_ids)

        return sessions

    async def _cleanup_orphaned_indexes(
        self, user_id: str, session_ids: list[str]
    ) -> None:
        """
        Remove orphaned index entries for sessions that no longer exist.

        Called when get_by_user encounters sessions that have expired via TTL.

        Args:
            user_id: The user whose session index should be cleaned
            session_ids: List of session IDs to remove from indexes
        """
        pipe = self._redis.pipeline()
        for session_id in session_ids:
            pipe.srem(f"user:{user_id}:sessions", session_id)
            pipe.zrem("sessions:by_updated_at", session_id)
        await pipe.execute()

    async def update_activity(self, jwt_token: str) -> bool:
        """
        Update session's updated_at timestamp.

        Called when session activity occurs (e.g., sync ack received).

        Args:
            jwt_token: The Gumnut JWT

        Returns:
            True if session exists and was updated, False otherwise
        """
        session_id = self.hash_jwt(jwt_token)
        if not await self._redis.exists(f"session:{session_id}"):
            return False

        now = datetime.now(timezone.utc)
        pipe = self._redis.pipeline()
        pipe.hset(f"session:{session_id}", "updated_at", now.isoformat())
        pipe.zadd("sessions:by_updated_at", {session_id: now.timestamp()})
        await pipe.execute()
        return True

    async def set_pending_sync_reset(self, session_id: str, pending: bool) -> bool:
        """
        Set the is_pending_sync_reset flag.

        When True, server sends SyncResetV1 message telling client
        to clear local data and full re-sync.

        Args:
            session_id: The hashed session ID
            pending: Whether sync reset is pending

        Returns:
            True if session exists and was updated, False otherwise
        """
        if not await self._redis.exists(f"session:{session_id}"):
            return False

        await self._redis.hset(
            f"session:{session_id}",
            "is_pending_sync_reset",
            "1" if pending else "0",
        )
        return True

    async def delete(self, jwt_token: str) -> bool:
        """
        Delete a session.

        Removes session data and all index entries.

        Args:
            jwt_token: The Gumnut JWT

        Returns:
            True if session existed and was deleted, False otherwise
        """
        session_id = self.hash_jwt(jwt_token)
        return await self.delete_by_id(session_id)

    async def delete_by_id(self, session_id: str) -> bool:
        """
        Delete a session by ID.

        Args:
            session_id: The hashed session ID

        Returns:
            True if session existed and was deleted, False otherwise
        """
        session = await self.get_by_id(session_id)
        if not session:
            return False

        pipe = self._redis.pipeline()
        pipe.delete(f"session:{session_id}")
        pipe.srem(f"user:{session.user_id}:sessions", session_id)
        pipe.zrem("sessions:by_updated_at", session_id)
        await pipe.execute()
        return True

    async def delete_all_for_user(self, user_id: str) -> int:
        """
        Delete all sessions for a user.

        Uses pipelining to delete all sessions efficiently.

        Args:
            user_id: Gumnut user ID

        Returns:
            Number of sessions deleted
        """
        session_ids = await self._redis.smembers(f"user:{user_id}:sessions")
        if not session_ids:
            return 0

        session_id_list = list(session_ids)

        # Delete all session data and indexes in one pipeline
        pipe = self._redis.pipeline()
        for session_id in session_id_list:
            pipe.delete(f"session:{session_id}")
            pipe.zrem("sessions:by_updated_at", session_id)
        # Clear the entire user sessions set
        pipe.delete(f"user:{user_id}:sessions")
        await pipe.execute()

        return len(session_id_list)

    async def get_stale_sessions(self, days: int = 90) -> list[str]:
        """
        Get session IDs that have been inactive for N days.

        Args:
            days: Number of days of inactivity

        Returns:
            List of stale session IDs
        """
        cutoff = time.time() - (days * 24 * 60 * 60)
        return list(
            await self._redis.zrangebyscore("sessions:by_updated_at", 0, cutoff)
        )

    async def cleanup_stale_sessions(self, days: int = 90) -> int:
        """
        Delete sessions inactive for N days.

        Uses pipelining to fetch session data efficiently before deletion.

        Args:
            days: Number of days of inactivity threshold

        Returns:
            Number of sessions deleted
        """
        stale_ids = await self.get_stale_sessions(days)
        if not stale_ids:
            return 0

        # Fetch all session data to get user_ids for index cleanup
        pipe = self._redis.pipeline()
        for session_id in stale_ids:
            pipe.hgetall(f"session:{session_id}")
        results = await pipe.execute()

        # Build deletion pipeline
        delete_pipe = self._redis.pipeline()
        count = 0

        for session_id, data in zip(stale_ids, results):
            if data:
                try:
                    session = Session.from_dict(session_id, data)
                    delete_pipe.delete(f"session:{session_id}")
                    delete_pipe.srem(f"user:{session.user_id}:sessions", session_id)
                    delete_pipe.zrem("sessions:by_updated_at", session_id)
                    count += 1
                except SessionDataError:
                    # Session data is corrupted, just remove what we can
                    delete_pipe.delete(f"session:{session_id}")
                    delete_pipe.zrem("sessions:by_updated_at", session_id)
                    count += 1
            else:
                # Session already expired via TTL, just clean up the sorted set entry
                delete_pipe.zrem("sessions:by_updated_at", session_id)

        if count > 0:
            await delete_pipe.execute()

        return count

    async def get_ttl(self, jwt_token: str) -> int | None:
        """
        Get remaining TTL for a session in seconds.

        Args:
            jwt_token: The Gumnut JWT

        Returns:
            Seconds remaining, None if no TTL set, or session doesn't exist
        """
        session_id = self.hash_jwt(jwt_token)
        ttl = await self._redis.ttl(f"session:{session_id}")
        if ttl == -2:  # Key doesn't exist
            return None
        if ttl == -1:  # No TTL set
            return None
        return ttl

    async def exists(self, jwt_token: str) -> bool:
        """
        Check if session exists.

        Args:
            jwt_token: The Gumnut JWT

        Returns:
            True if session exists
        """
        session_id = self.hash_jwt(jwt_token)
        return await self._redis.exists(f"session:{session_id}") > 0


async def get_session_store() -> SessionStore:
    """
    FastAPI dependency that provides a SessionStore instance.

    Usage:
        @app.get("/sessions")
        async def get_sessions(
            store: SessionStore = Depends(get_session_store)
        ):
            ...

    Returns:
        SessionStore configured with the singleton Redis client
    """
    from utils.redis_client import get_redis_client

    redis_client = await get_redis_client()
    return SessionStore(redis_client)
