"""Checkpoint storage service for sync progress tracking."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import sentry_sdk

from routers.immich_models import SyncEntityType
from services.session_store import checkpoint_key
from utils.redis_client import get_redis_client, parse_redis_peer
from utils.redis_protocols import AsyncRedisClient

logger = logging.getLogger(__name__)


class CheckpointDataError(Exception):
    """Raised when checkpoint data from Redis is invalid or corrupted."""

    pass


@dataclass
class Checkpoint:
    """
    Checkpoint data for a sync entity type.

    Tracks sync progress for a specific entity type (e.g., AssetV1, AlbumV1).
    Each session maintains independent checkpoints per entity type.
    """

    entity_type: SyncEntityType
    updated_at: datetime  # When checkpoint was stored (for activity tracking)
    cursor: str | None = None  # Opaque v2 events cursor

    def to_redis_value(self) -> str:
        """
        Convert to Redis storage format.

        Returns:
            Pipe-delimited string: "{updated_at}|{cursor}"
            - updated_at: When this checkpoint was stored (used for activity tracking)
            - cursor: Opaque v2 events cursor (empty string if None)
        """
        cursor = self.cursor or ""
        return f"{self.updated_at.isoformat()}|{cursor}"

    @classmethod
    def from_redis_value(cls, entity_type: SyncEntityType, value: str) -> "Checkpoint":
        """
        Create from Redis stored value.

        Args:
            entity_type: The entity type (hash field name)
            value: Pipe-delimited string from Redis

        Returns:
            Checkpoint object

        Raises:
            CheckpointDataError: If value is malformed
        """
        parts = value.split("|")
        if len(parts) != 2:
            raise CheckpointDataError(
                f"Checkpoint for {entity_type.value} with value {value} has invalid format: expected 2 parts, got {len(parts)}"
            )

        try:
            updated_at = datetime.fromisoformat(parts[0])
        except ValueError as e:
            raise CheckpointDataError(
                f"Checkpoint for {entity_type.value} has invalid timestamp: {e}"
            ) from e

        # Parse cursor (2nd field), treat empty string as None
        cursor = parts[1] if parts[1] else None

        return cls(
            entity_type=entity_type,
            updated_at=updated_at,
            cursor=cursor,
        )


# KEYS: session hash, the epoch's checkpoint key. ARGV: the epoch, then
# field/value pairs. Writes only while the session is still at that epoch, so
# an ack from a stream of a library the session has left is dropped, and gives
# the checkpoints the session's remaining TTL so they expire with it.
_SET_IF_EPOCH_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
if (redis.call('HGET', KEYS[1], 'sync_epoch') or '0') ~= ARGV[1] then
  return 0
end
redis.call('HSET', KEYS[2], unpack(ARGV, 2))
local ttl = redis.call('PTTL', KEYS[1])
if ttl > 0 then
  redis.call('PEXPIRE', KEYS[2], ttl)
end
return 1
"""


class CheckpointStore:
    """
    Abstraction layer for checkpoint storage.

    Hides Redis implementation details from calling code.
    All checkpoint operations go through this class.

    Checkpoints are tied to a session's sync epoch (see SessionStore): each
    epoch's checkpoints are a hash of entity type to "{updated_at}|{cursor}".
    SessionStore deletes them with the session or when the epoch advances.
    """

    def __init__(self, redis_client: Any):
        """
        Initialize CheckpointStore with a Redis client.

        Args:
            redis_client: An async Redis client (redis.asyncio.Redis)
        """
        self._redis: AsyncRedisClient = redis_client
        self._redis_host, self._redis_port = parse_redis_peer()

    def _set_network_data(self, span: Any) -> None:
        """Set network peer attributes on a cache span."""
        span.set_data("network.peer.address", self._redis_host)
        span.set_data("network.peer.port", self._redis_port)

    async def get_all(self, session_token: UUID, epoch: int) -> list[Checkpoint]:
        """
        Get all checkpoints for a session's sync epoch.

        Args:
            session_token: The session token (UUID)
            epoch: The session's sync epoch

        Returns:
            List of Checkpoint objects, empty if none exist
        """
        key = checkpoint_key(session_token, epoch)
        with sentry_sdk.start_span(op="cache.get", name="checkpoint") as span:
            data = await self._redis.hgetall(key)
            span.set_data("cache.key", [key])
            span.set_data("cache.hit", bool(data))
            self._set_network_data(span)
            if data:
                span.set_data("cache.item_size", sum(len(v) for v in data.values()))
        if not data:
            return []

        checkpoints = []
        for entity_type_str, value in data.items():
            entity_type = SyncEntityType(entity_type_str)
            try:
                checkpoint = Checkpoint.from_redis_value(entity_type, value)
            except CheckpointDataError:
                logger.warning(
                    "Skipping corrupt checkpoint (old format?), will re-sync",
                    extra={
                        "session_token": str(session_token),
                        "entity_type": entity_type_str,
                        "value": value,
                    },
                )
                continue
            checkpoints.append(checkpoint)

        return checkpoints

    async def get(
        self, session_token: UUID, epoch: int, entity_type: SyncEntityType
    ) -> Checkpoint | None:
        """
        Get a specific checkpoint for a session's sync epoch.

        Args:
            session_token: The session token (UUID)
            epoch: The session's sync epoch
            entity_type: The entity type

        Returns:
            Checkpoint if found, None otherwise
        """
        key = checkpoint_key(session_token, epoch)
        with sentry_sdk.start_span(op="cache.get", name="checkpoint") as span:
            value = await self._redis.hget(key, entity_type.value)
            span.set_data("cache.key", [key])
            span.set_data("cache.hit", value is not None)
            self._set_network_data(span)
            if value:
                span.set_data("cache.item_size", len(value))
        if not value:
            return None

        try:
            return Checkpoint.from_redis_value(entity_type, value)
        except CheckpointDataError:
            logger.warning(
                "Skipping corrupt checkpoint (old format?), will re-sync",
                extra={
                    "session_token": str(session_token),
                    "entity_type": entity_type.value,
                    "value": value,
                },
            )
            return None

    async def set_many(
        self,
        session_token: UUID,
        epoch: int,
        checkpoints: list[tuple[SyncEntityType, str]],
    ) -> bool:
        """
        Set multiple checkpoints for a sync epoch atomically, only while the
        session is still at that epoch.

        Args:
            session_token: The session token (UUID)
            epoch: The sync epoch the acked events were streamed for
            checkpoints: List of (entity_type, cursor) tuples.

        Returns:
            Whether the checkpoints were written
        """
        if not checkpoints:
            return True

        now = datetime.now(timezone.utc)
        mapping: dict[str, str] = {}

        for entity_type, cursor in checkpoints:
            checkpoint = Checkpoint(
                entity_type=entity_type,
                updated_at=now,
                cursor=cursor,
            )
            mapping[entity_type.value] = checkpoint.to_redis_value()

        key = checkpoint_key(session_token, epoch)
        args = [str(epoch)]
        for field, value in mapping.items():
            args.extend((field, value))
        with sentry_sdk.start_span(op="cache.put", name="checkpoint") as span:
            written = await self._redis.eval(
                _SET_IF_EPOCH_LUA, 2, f"session:{session_token}", key, *args
            )
            span.set_data("cache.key", [key])
            span.set_data("cache.item_size", sum(len(v) for v in mapping.values()))
            self._set_network_data(span)
        return bool(written)

    async def delete(
        self, session_token: UUID, epoch: int, entity_types: list[SyncEntityType]
    ) -> bool:
        """
        Delete specific checkpoints for a session's sync epoch.

        Args:
            session_token: The session token (UUID)
            epoch: The session's sync epoch
            entity_types: List of entity types to delete

        Returns:
            True if operation completed (even if no checkpoints existed)
        """
        if not entity_types:
            return True

        entity_type_values = [et.value for et in entity_types]
        await self._redis.hdel(
            checkpoint_key(session_token, epoch), *entity_type_values
        )
        return True

    async def delete_all(self, session_token: UUID, epoch: int) -> bool:
        """
        Delete all checkpoints for a session's sync epoch.

        Args:
            session_token: The session token (UUID)
            epoch: The session's sync epoch

        Returns:
            True if operation completed (even if no checkpoints existed)
        """
        await self._redis.delete(checkpoint_key(session_token, epoch))
        return True


async def get_checkpoint_store() -> CheckpointStore:
    """
    FastAPI dependency that provides a CheckpointStore instance.

    Returns:
        CheckpointStore configured with the singleton Redis client
    """
    redis_client = await get_redis_client()
    return CheckpointStore(redis_client)
