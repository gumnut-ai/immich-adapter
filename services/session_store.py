import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import redis.exceptions

from utils.jwt_encryption import decrypt_jwt, encrypt_jwt
from utils.redis_client import get_redis_client
from utils.redis_protocols import AsyncRedisClient


class SessionStoreError(Exception):
    """Raised when session store operations fail (e.g., connectivity issues)."""

    pass


class SessionDataError(Exception):
    """Raised when session data from Redis is invalid or corrupted."""

    pass


class SessionExpiredError(ValueError):
    """Raised when attempting to create a session with an expiration time in the past."""

    pass


_REQUIRED_SESSION_FIELDS = frozenset(
    [
        "user_id",
        "library_id",
        "stored_jwt",
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
    """Session data stored in Redis."""

    id: UUID  # The session token (what client sends as accessToken)
    user_id: str  # Gumnut user ID (UUID format)
    library_id: str  # User's default library (or empty string)
    stored_jwt: str  # Encrypted Gumnut JWT
    device_type: str  # "iOS", "Android", "Chrome", etc.
    device_os: str  # "iOS", "macOS", "Android", etc.
    app_version: str  # "1.94.0" or empty for web
    created_at: datetime  # When session was created
    updated_at: datetime  # Last activity timestamp
    is_pending_sync_reset: bool  # True = client should full re-sync

    def to_dict(self) -> dict[str, str]:
        """Convert to Redis hash format."""
        return {
            "user_id": self.user_id,
            "library_id": self.library_id,
            "stored_jwt": self.stored_jwt,
            "device_type": self.device_type,
            "device_os": self.device_os,
            "app_version": self.app_version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "is_pending_sync_reset": "1" if self.is_pending_sync_reset else "0",
        }

    @classmethod
    def from_dict(cls, session_id: UUID, data: dict[str, str]) -> "Session":
        """
        Create from Redis hash data.

        Args:
            session_id: The session UUID
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
                stored_jwt=data["stored_jwt"],
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

    def get_jwt(self) -> str:
        """
        Decrypt and return the stored JWT.

        Returns:
            The decrypted Gumnut JWT

        Raises:
            JWTEncryptionError: If decryption fails
        """
        return decrypt_jwt(self.stored_jwt)


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

        Generates a unique session token (UUID), encrypts the JWT for storage,
        and stores the session in Redis. The session token is returned to clients
        as their access token, decoupling the session from the JWT.

        Args:
            jwt_token: The Gumnut JWT (will be encrypted and stored)
            user_id: Gumnut user ID from JWT claims
            library_id: User's default library ID
            device_type: "iOS", "Android", "Chrome", etc.
            device_os: "iOS 17.4", "Android 13", etc.
            app_version: "1.94.0" or empty string for web
            expires_at: Optional expiration time (uses Redis TTL)

        Returns:
            The created Session object (session.id is the token to return to client)

        Raises:
            SessionExpiredError: If expires_at is in the past
            JWTEncryptionError: If JWT encryption fails
        """
        session_id = uuid4()
        now = datetime.now(timezone.utc)

        if expires_at is not None and expires_at <= now:
            raise SessionExpiredError(
                f"Cannot create session with expiration time in the past: {expires_at}"
            )

        # Encrypt the JWT for secure storage
        encrypted_jwt = encrypt_jwt(jwt_token)

        session = Session(
            id=session_id,
            user_id=user_id,
            library_id=library_id,
            stored_jwt=encrypted_jwt,
            device_type=device_type,
            device_os=device_os,
            app_version=app_version,
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )

        session_key = str(session_id)
        pipe = self._redis.pipeline()
        pipe.hset(f"session:{session_key}", mapping=session.to_dict())
        pipe.sadd(f"user:{user_id}:sessions", session_key)
        pipe.zadd("sessions:by_updated_at", {session_key: now.timestamp()})

        if expires_at is not None:
            ttl_seconds = int((expires_at - now).total_seconds())
            if ttl_seconds <= 0:
                # Should have been caught by the earlier `expires_at <= now` check,
                # but guard against rounding issues.
                ttl_seconds = 1
            pipe.expire(f"session:{session_key}", ttl_seconds)
            # Set same TTL on checkpoint key so it expires with the session
            pipe.expire(f"session:{session_key}:checkpoints", ttl_seconds)

        await pipe.execute()
        return session

    async def get_by_id(self, session_token: str) -> Session | None:
        """
        Get session by session token.

        Args:
            session_token: The session token (UUID string)

        Returns:
            Session if found, None otherwise

        Raises:
            SessionStoreError: If Redis operation fails
            SessionDataError: If session data is corrupted
        """
        try:
            session_uuid = UUID(session_token)
        except ValueError:
            return None

        try:
            data = await self._redis.hgetall(f"session:{session_token}")
        except redis.exceptions.RedisError as e:
            raise SessionStoreError(f"Failed to retrieve session: {e}") from e

        if not data:
            return None

        return Session.from_dict(session_uuid, data)

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
        session_tokens = await self._redis.smembers(f"user:{user_id}:sessions")
        if not session_tokens:
            return []

        # Fetch all sessions in one pipeline
        session_token_list = list(session_tokens)
        pipe = self._redis.pipeline()
        for session_token in session_token_list:
            pipe.hgetall(f"session:{session_token}")
        results = await pipe.execute()

        sessions = []
        orphaned_tokens = []

        for session_token, data in zip(session_token_list, results):
            if data:
                try:
                    session_uuid = UUID(session_token)
                    sessions.append(Session.from_dict(session_uuid, data))
                except (SessionDataError, ValueError):
                    # Treat corrupted sessions as orphaned
                    orphaned_tokens.append(session_token)
            else:
                # Session expired via TTL, mark for cleanup
                orphaned_tokens.append(session_token)

        # Clean up orphaned index entries
        if orphaned_tokens:
            await self._cleanup_orphaned_indexes(user_id, orphaned_tokens)

        return sessions

    async def _cleanup_orphaned_indexes(
        self, user_id: str, session_tokens: list[str]
    ) -> None:
        """
        Remove orphaned index entries for sessions that no longer exist.

        Called when get_by_user encounters sessions that have expired via TTL.

        Args:
            user_id: The user whose session index should be cleaned
            session_tokens: List of session tokens to remove from indexes
        """
        pipe = self._redis.pipeline()
        for session_token in session_tokens:
            pipe.srem(f"user:{user_id}:sessions", session_token)
            pipe.zrem("sessions:by_updated_at", session_token)
        await pipe.execute()

    async def update_activity(self, session_token: str) -> bool:
        """
        Update session's updated_at timestamp.

        Called when session activity occurs (e.g., sync ack received).

        Args:
            session_token: The session token (UUID string)

        Returns:
            True if session exists and was updated, False otherwise
        """
        if not await self._redis.exists(f"session:{session_token}"):
            return False

        now = datetime.now(timezone.utc)
        pipe = self._redis.pipeline()
        pipe.hset(f"session:{session_token}", "updated_at", now.isoformat())
        pipe.zadd("sessions:by_updated_at", {session_token: now.timestamp()})
        await pipe.execute()
        return True

    async def update_stored_jwt(self, session_token: str, new_jwt: str) -> bool:
        """
        Update the stored JWT for a session.

        Called when the Gumnut backend refreshes the JWT. The session token
        remains the same, but the stored JWT is updated.

        Args:
            session_token: The session token (UUID string)
            new_jwt: The new JWT to encrypt and store

        Returns:
            True if session exists and was updated, False otherwise

        Raises:
            JWTEncryptionError: If JWT encryption fails
        """
        if not await self._redis.exists(f"session:{session_token}"):
            return False

        encrypted_jwt = encrypt_jwt(new_jwt)
        now = datetime.now(timezone.utc)

        pipe = self._redis.pipeline()
        pipe.hset(f"session:{session_token}", "stored_jwt", encrypted_jwt)
        pipe.hset(f"session:{session_token}", "updated_at", now.isoformat())
        pipe.zadd("sessions:by_updated_at", {session_token: now.timestamp()})
        await pipe.execute()
        return True

    async def set_pending_sync_reset(self, session_token: str, pending: bool) -> bool:
        """
        Set the is_pending_sync_reset flag.

        When True, server sends SyncResetV1 message telling client
        to clear local data and full re-sync.

        Args:
            session_token: The session token (UUID string)
            pending: Whether sync reset is pending

        Returns:
            True if session exists and was updated, False otherwise
        """
        if not await self._redis.exists(f"session:{session_token}"):
            return False

        await self._redis.hset(
            f"session:{session_token}",
            "is_pending_sync_reset",
            "1" if pending else "0",
        )
        return True

    async def delete(self, session_token: str) -> bool:
        """
        Delete a session.

        Removes session data and all index entries.

        Args:
            session_token: The session token (UUID string)

        Returns:
            True if session existed and was deleted, False otherwise
        """
        return await self.delete_by_id(session_token)

    async def delete_by_id(self, session_token: str) -> bool:
        """
        Delete a session by token.

        Removes session data, checkpoint data, and all index entries.

        Args:
            session_token: The session token (UUID string)

        Returns:
            True if session existed and was deleted, False otherwise
        """
        session = await self.get_by_id(session_token)
        if not session:
            return False

        pipe = self._redis.pipeline()
        pipe.delete(f"session:{session_token}")
        pipe.delete(f"session:{session_token}:checkpoints")
        pipe.srem(f"user:{session.user_id}:sessions", session_token)
        pipe.zrem("sessions:by_updated_at", session_token)
        await pipe.execute()
        return True

    async def delete_all_for_user(self, user_id: str) -> int:
        """
        Delete all sessions for a user.

        Uses pipelining to delete all sessions and their checkpoints efficiently.

        Args:
            user_id: Gumnut user ID

        Returns:
            Number of sessions deleted
        """
        session_tokens = await self._redis.smembers(f"user:{user_id}:sessions")
        if not session_tokens:
            return 0

        session_token_list = list(session_tokens)

        # Delete all session data, checkpoints, and indexes in one pipeline
        pipe = self._redis.pipeline()
        for session_token in session_token_list:
            pipe.delete(f"session:{session_token}")
            pipe.delete(f"session:{session_token}:checkpoints")
            pipe.zrem("sessions:by_updated_at", session_token)
        # Clear the entire user sessions set
        pipe.delete(f"user:{user_id}:sessions")
        await pipe.execute()

        return len(session_token_list)

    async def get_stale_sessions(self, days: int = 90) -> list[str]:
        """
        Get session tokens that have been inactive for N days.

        Args:
            days: Number of days of inactivity

        Returns:
            List of stale session tokens
        """
        cutoff = time.time() - (days * 24 * 60 * 60)
        return list(
            await self._redis.zrangebyscore("sessions:by_updated_at", 0, cutoff)
        )

    async def cleanup_stale_sessions(self, days: int = 90) -> int:
        """
        Delete sessions inactive for N days.

        Uses pipelining to fetch session data efficiently before deletion.
        Also deletes associated checkpoint data.

        Args:
            days: Number of days of inactivity threshold

        Returns:
            Number of sessions deleted
        """
        stale_tokens = await self.get_stale_sessions(days)
        if not stale_tokens:
            return 0

        # Fetch all session data to get user_ids for index cleanup
        pipe = self._redis.pipeline()
        for session_token in stale_tokens:
            pipe.hgetall(f"session:{session_token}")
        results = await pipe.execute()

        # Build deletion pipeline
        delete_pipe = self._redis.pipeline()
        count = 0

        for session_token, data in zip(stale_tokens, results):
            if data:
                try:
                    session_uuid = UUID(session_token)
                    session = Session.from_dict(session_uuid, data)
                    delete_pipe.delete(f"session:{session_token}")
                    delete_pipe.delete(f"session:{session_token}:checkpoints")
                    delete_pipe.srem(f"user:{session.user_id}:sessions", session_token)
                    delete_pipe.zrem("sessions:by_updated_at", session_token)
                    count += 1
                except (SessionDataError, ValueError):
                    # Session data is corrupted, just remove what we can
                    delete_pipe.delete(f"session:{session_token}")
                    delete_pipe.delete(f"session:{session_token}:checkpoints")
                    delete_pipe.zrem("sessions:by_updated_at", session_token)
                    count += 1
            else:
                # Session already expired via TTL, just clean up the sorted set entry
                # Checkpoint key would have expired with same TTL
                delete_pipe.zrem("sessions:by_updated_at", session_token)

        if count > 0:
            await delete_pipe.execute()

        return count

    async def get_ttl(self, session_token: str) -> int | None:
        """
        Get remaining TTL for a session in seconds.

        Args:
            session_token: The session token (UUID string)

        Returns:
            Seconds remaining, None if no TTL set, or session doesn't exist
        """
        ttl = await self._redis.ttl(f"session:{session_token}")
        if ttl == -2:  # Key doesn't exist
            return None
        if ttl == -1:  # No TTL set
            return None
        return ttl

    async def exists(self, session_token: str) -> bool:
        """
        Check if session exists.

        Args:
            session_token: The session token (UUID string)

        Returns:
            True if session exists
        """
        return await self._redis.exists(f"session:{session_token}") > 0


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
    redis_client = await get_redis_client()
    return SessionStore(redis_client)
