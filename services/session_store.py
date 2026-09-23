import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import redis.exceptions
import sentry_sdk

from utils.jwt_encryption import decrypt_jwt, encrypt_jwt
from utils.redis_client import get_redis_client, parse_redis_peer
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


# KEYS: session hash, activity index. ARGV: session token, activity score or
# "" to skip the index write, then field/value pairs.
_UPDATE_IF_EXISTS_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
if #ARGV > 2 then
  redis.call('HSET', KEYS[1], unpack(ARGV, 3))
end
if ARGV[2] ~= '' then
  redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
end
return 1
"""

# Sync state (checkpoints, and whether the client must reset) belongs to the
# session's sync epoch. Only a change of library advances it, so a checkpoint
# written for one epoch can never be read back for another library. Writers
# also keep is_pending_sync_reset, which sessions carried before epochs, in
# step so an adapter still running that version (a rolling deploy or rollback)
# reads the session and still owes the reset.

# KEYS: session hash, epoch-0 checkpoint key. ARGV: the library_id the caller
# observed, the library_id it wants, then field/value pairs. Writes only while
# the session still holds the observed library (every session hash has the
# field), so a request that read stale state cannot overwrite a concurrent
# change. Leaving a library advances the epoch and drops the old epoch's
# checkpoints. Returns whether the session now holds the wanted library.
_SET_LIBRARY_IF_LUA = """
local current = redis.call('HGET', KEYS[1], 'library_id')
if current ~= ARGV[1] then
  return current == ARGV[2] and 1 or 0
end
redis.call('HSET', KEYS[1], unpack(ARGV, 3))
if current ~= '' and current ~= ARGV[2] then
  local epoch = tonumber(redis.call('HGET', KEYS[1], 'sync_epoch') or '0')
  local old = KEYS[2]
  if epoch > 0 then old = old .. ':' .. epoch end
  redis.call('DEL', old)
  redis.call('HSET', KEYS[1], 'sync_epoch', epoch + 1, 'is_pending_sync_reset', '1')
end
return 1
"""

# KEYS: session hash, epoch-0 checkpoint key. Advances the epoch without
# changing library, so the client resets and resyncs from empty checkpoints.
_ADVANCE_EPOCH_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
local epoch = tonumber(redis.call('HGET', KEYS[1], 'sync_epoch') or '0')
local old = KEYS[2]
if epoch > 0 then old = old .. ':' .. epoch end
redis.call('DEL', old)
redis.call('HSET', KEYS[1], 'sync_epoch', epoch + 1, 'is_pending_sync_reset', '1')
return 1
"""

# KEYS: session hash, the epoch's checkpoint key. ARGV: the epoch the client
# reset to. The client acks only after wiping its local copy, so its
# checkpoints go too; the client's copy now belongs to that epoch. An ack for
# an epoch the session has left changes nothing: the client resets again.
_ACK_RESET_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
if (redis.call('HGET', KEYS[1], 'sync_epoch') or '0') ~= ARGV[1] then
  return 0
end
redis.call('DEL', KEYS[2])
redis.call('HSET', KEYS[1], 'client_epoch', ARGV[1], 'is_pending_sync_reset', '0')
return 1
"""

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
    ]
)


@dataclass
class Session:
    """Session data stored in Redis."""

    id: UUID  # The session token (what client sends as accessToken)
    user_id: str  # Gumnut user ID (UUID format)
    library_id: str  # Resolved Gumnut library, or "" until the first scoped request
    stored_jwt: str  # Encrypted Gumnut JWT
    device_type: str  # "iOS", "Android", "Chrome", etc.
    device_os: str  # "iOS", "macOS", "Android", etc.
    app_version: str  # "1.94.0" or empty for web
    created_at: datetime  # When session was created
    updated_at: datetime  # Last activity timestamp
    # Advances when the session leaves a library; checkpoints belong to one epoch.
    sync_epoch: int = 0
    # The epoch the client's local copy belongs to; the client must reset
    # (SyncResetV1) while it differs from sync_epoch.
    client_epoch: int = 0
    # When library_id was last resolved (epoch seconds); 0 means never, so a
    # session written before this field existed revalidates on its next request.
    library_checked_at: float = 0.0
    # Whether library_id is the user's stored choice rather than the fallback.
    library_from_choice: bool = False

    @property
    def is_pending_sync_reset(self) -> bool:
        return self.client_epoch != self.sync_epoch

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
            "sync_epoch": str(self.sync_epoch),
            "client_epoch": str(self.client_epoch),
            "is_pending_sync_reset": "1" if self.is_pending_sync_reset else "0",
            "library_checked_at": str(self.library_checked_at),
            "library_from_choice": "1" if self.library_from_choice else "0",
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
            sync_epoch = int(data.get("sync_epoch") or 0)
            client_epoch = data.get("client_epoch")
            if client_epoch is None:
                # The client's copy predates epochs, so it belongs to epoch 0,
                # and a reset flagged then is still owed.
                pending = data.get("is_pending_sync_reset") == "1"
                client_epoch = -1 if pending else 0
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
                sync_epoch=sync_epoch,
                client_epoch=int(client_epoch),
                library_checked_at=float(data.get("library_checked_at") or 0),
                library_from_choice=data.get("library_from_choice") == "1",
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
        self._redis_host, self._redis_port = parse_redis_peer()

    def _set_network_data(self, span: Any) -> None:
        """Set network peer attributes on a cache span."""
        span.set_data("network.peer.address", self._redis_host)
        span.set_data("network.peer.port", self._redis_port)

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
            library_id: Resolved Gumnut library, or "" to resolve on first use
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
        )

        session_key = str(session_id)
        session_data = session.to_dict()
        cache_key = f"session:{session_key}"

        with sentry_sdk.start_span(op="cache.put", name="session") as span:
            pipe = self._redis.pipeline()
            pipe.hset(cache_key, mapping=session_data)
            pipe.sadd(f"user:{user_id}:sessions", session_key)
            pipe.zadd("sessions:by_updated_at", {session_key: now.timestamp()})

            if expires_at is not None:
                ttl_seconds = int((expires_at - now).total_seconds())
                if ttl_seconds <= 0:
                    # Should have been caught by the earlier `expires_at <= now` check,
                    # but guard against rounding issues.
                    ttl_seconds = 1
                pipe.expire(cache_key, ttl_seconds)

            await pipe.execute()
            span.set_data("cache.key", [cache_key])
            span.set_data("cache.item_size", sum(len(v) for v in session_data.values()))
            self._set_network_data(span)
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

        cache_key = f"session:{session_token}"
        try:
            with sentry_sdk.start_span(op="cache.get", name="session") as span:
                data = await self._redis.hgetall(cache_key)
                span.set_data("cache.key", [cache_key])
                span.set_data("cache.hit", bool(data))
                self._set_network_data(span)
                if data:
                    span.set_data("cache.item_size", sum(len(v) for v in data.values()))
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

    async def _update_if_exists(
        self,
        session_token: str,
        fields: dict[str, str] | None = None,
        *,
        touch_activity: bool = False,
    ) -> bool:
        """Write hash fields atomically, only if the session still exists."""
        updates = dict(fields or {})
        score = ""
        if touch_activity:
            now = datetime.now(timezone.utc)
            updates["updated_at"] = now.isoformat()
            score = str(now.timestamp())

        args = [session_token, score]
        for field, value in updates.items():
            args.extend((field, value))

        return bool(
            await self._redis.eval(
                _UPDATE_IF_EXISTS_LUA,
                2,
                f"session:{session_token}",
                "sessions:by_updated_at",
                *args,
            )
        )

    async def update_activity(self, session_token: str) -> bool:
        """
        Update session's updated_at timestamp.

        Called when session activity occurs (e.g., sync ack received).

        Args:
            session_token: The session token (UUID string)

        Returns:
            True if session exists and was updated, False otherwise
        """
        return await self._update_if_exists(session_token, touch_activity=True)

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
        return await self._update_if_exists(
            session_token, {"stored_jwt": encrypt_jwt(new_jwt)}, touch_activity=True
        )

    async def set_library(
        self,
        session_token: str,
        previous_library_id: str,
        library_id: str,
        *,
        from_choice: bool = False,
    ) -> bool:
        """
        Record the library the session's Gumnut calls are scoped to (``""``
        drops it), stamped with the time it was resolved. Leaving a library
        advances the sync epoch, which resets the client's sync: its local copy
        and checkpoints belong to the previous library. The first resolution
        (from ``""``) keeps the epoch.

        Returns:
            Whether the session now holds ``library_id``: written from
            ``previous_library_id``, or already moved there by another request
        """
        fields = {
            "library_id": library_id,
            "library_checked_at": str(time.time()),
            "library_from_choice": "1" if from_choice else "0",
        }
        args = [previous_library_id, library_id]
        for field, value in fields.items():
            args.extend((field, value))
        return bool(
            await self._redis.eval(
                _SET_LIBRARY_IF_LUA,
                2,
                f"session:{session_token}",
                checkpoint_key(session_token, 0),
                *args,
            )
        )

    async def set_pending_sync_reset(self, session_token: str, pending: bool) -> bool:
        """
        Set whether the client must reset (SyncResetV1) and resync from
        scratch. Requesting a reset advances the sync epoch without changing
        library; clearing it marks the client current with the session's epoch.

        Returns:
            True if session exists and was updated, False otherwise
        """
        if pending:
            return bool(
                await self._redis.eval(
                    _ADVANCE_EPOCH_LUA,
                    2,
                    f"session:{session_token}",
                    checkpoint_key(session_token, 0),
                )
            )
        session = await self.get_by_id(session_token)
        if session is None:
            return False
        # Not atomic with the read: if the epoch advances meanwhile, the stale
        # value still differs from it and the reset stays owed.
        return await self._update_if_exists(
            session_token,
            {"client_epoch": str(session.sync_epoch), "is_pending_sync_reset": "0"},
        )

    async def acknowledge_sync_reset(self, session_token: str, epoch: int) -> bool:
        """
        Record that the client reset its local copy for ``epoch``.

        Returns:
            Whether the session is still at ``epoch``
        """
        return bool(
            await self._redis.eval(
                _ACK_RESET_LUA,
                2,
                f"session:{session_token}",
                checkpoint_key(session_token, epoch),
                str(epoch),
            )
        )

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
        pipe.delete(checkpoint_key(session_token, session.sync_epoch))
        pipe.srem(f"user:{session.user_id}:sessions", session_token)
        pipe.zrem("sessions:by_updated_at", session_token)
        await pipe.execute()
        return True

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
                    delete_pipe.delete(
                        checkpoint_key(session_token, session.sync_epoch)
                    )
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


def checkpoint_key(session_token: object, epoch: int) -> str:
    """Redis key of a session's checkpoints for one sync epoch. Epoch 0 keeps
    the key used before epochs, so clients syncing at the upgrade keep their
    progress."""
    base = f"session:{session_token}:checkpoints"
    return f"{base}:{epoch}" if epoch else base


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
