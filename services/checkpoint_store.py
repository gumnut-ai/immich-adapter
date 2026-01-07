"""Checkpoint storage service for sync progress tracking."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from routers.immich_models import SyncEntityType
from utils.redis_client import get_redis_client
from utils.redis_protocols import AsyncRedisClient


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
    last_synced_at: datetime  # Timestamp from client ack (for query filtering)
    updated_at: datetime  # When checkpoint was stored (for activity tracking)
    last_entity_id: str | None = None  # Entity ID for keyset pagination

    def to_redis_value(self) -> str:
        """
        Convert to Redis storage format.

        Returns:
            Pipe-delimited string: "{last_synced_at}|{updated_at}|{last_entity_id}"
            - last_synced_at: Timestamp from client ack (used for query filtering)
            - updated_at: When this checkpoint was stored (used for activity tracking)
            - last_entity_id: Entity ID for keyset pagination (empty string if None)
        """
        entity_id = self.last_entity_id or ""
        return f"{self.last_synced_at.isoformat()}|{self.updated_at.isoformat()}|{entity_id}"

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

        Note:
            Backward compatible with old 2-field format (without entity_id).
        """
        parts = value.split("|")
        if len(parts) < 2 or len(parts) > 3:
            raise CheckpointDataError(
                f"Checkpoint for {entity_type.value} with value {value} has invalid format: expected 2-3 parts, got {len(parts)}"
            )

        try:
            last_synced_at = datetime.fromisoformat(parts[0])
            updated_at = datetime.fromisoformat(parts[1])
        except ValueError as e:
            raise CheckpointDataError(
                f"Checkpoint for {entity_type.value} has invalid timestamp: {e}"
            ) from e

        # Parse entity_id (3rd field) if present, treat empty string as None
        last_entity_id = None
        if len(parts) == 3 and parts[2]:
            last_entity_id = parts[2]

        return cls(
            entity_type=entity_type,
            last_synced_at=last_synced_at,
            updated_at=updated_at,
            last_entity_id=last_entity_id,
        )


def _checkpoint_key(session_token: UUID) -> str:
    """
    Generate Redis key for session checkpoints.

    Schema: session:{uuid}:checkpoints (Hash)
        Each field is an entity type (e.g., AssetV1, AlbumV1)
        Each value is pipe-delimited: {last_synced_at}|{updated_at}|{last_entity_id}
    """
    return f"session:{session_token}:checkpoints"


class CheckpointStore:
    """
    Abstraction layer for checkpoint storage.

    Hides Redis implementation details from calling code.
    All checkpoint operations go through this class.

    Checkpoints are tied to sessions - when a session is deleted,
    its checkpoints should also be deleted (handled by SessionStore).
    """

    def __init__(self, redis_client: Any):
        """
        Initialize CheckpointStore with a Redis client.

        Args:
            redis_client: An async Redis client (redis.asyncio.Redis)
        """
        self._redis: AsyncRedisClient = redis_client

    async def get_all(self, session_token: UUID) -> list[Checkpoint]:
        """
        Get all checkpoints for a session.

        Args:
            session_token: The session token (UUID)

        Returns:
            List of Checkpoint objects, empty if none exist
        """
        data = await self._redis.hgetall(_checkpoint_key(session_token))
        if not data:
            return []

        checkpoints = []
        for entity_type_str, value in data.items():
            entity_type = SyncEntityType(entity_type_str)
            checkpoint = Checkpoint.from_redis_value(entity_type, value)
            checkpoints.append(checkpoint)

        return checkpoints

    async def get(
        self, session_token: UUID, entity_type: SyncEntityType
    ) -> Checkpoint | None:
        """
        Get a specific checkpoint for a session.

        Args:
            session_token: The session token (UUID)
            entity_type: The entity type

        Returns:
            Checkpoint if found, None otherwise
        """
        value = await self._redis.hget(
            _checkpoint_key(session_token), entity_type.value
        )
        if not value:
            return None

        return Checkpoint.from_redis_value(entity_type, value)

    async def set(
        self,
        session_token: UUID,
        entity_type: SyncEntityType,
        last_synced_at: datetime,
        last_entity_id: str,
    ) -> bool:
        """
        Set a checkpoint for a session.

        Args:
            session_token: The session token (UUID)
            entity_type: The entity type
            last_synced_at: The sync timestamp from client ack
            last_entity_id: The entity ID for keyset pagination

        Returns:
            True if checkpoint was set successfully
        """
        now = datetime.now(timezone.utc)
        checkpoint = Checkpoint(
            entity_type=entity_type,
            last_synced_at=last_synced_at,
            updated_at=now,
            last_entity_id=last_entity_id,
        )

        await self._redis.hset(
            _checkpoint_key(session_token),
            entity_type.value,
            checkpoint.to_redis_value(),
        )
        return True

    async def set_many(
        self,
        session_token: UUID,
        checkpoints: list[tuple[SyncEntityType, datetime, str]],
    ) -> bool:
        """
        Set multiple checkpoints for a session atomically.

        Args:
            session_token: The session token (UUID)
            checkpoints: List of (entity_type, last_synced_at, last_entity_id) tuples).

        Returns:
            True if checkpoints were set successfully
        """
        if not checkpoints:
            return True

        now = datetime.now(timezone.utc)
        mapping: dict[str, str] = {}

        for entity_type, last_synced_at, last_entity_id in checkpoints:
            checkpoint = Checkpoint(
                entity_type=entity_type,
                last_synced_at=last_synced_at,
                updated_at=now,
                last_entity_id=last_entity_id,
            )
            mapping[entity_type.value] = checkpoint.to_redis_value()

        await self._redis.hset(_checkpoint_key(session_token), mapping=mapping)
        return True

    async def delete(
        self, session_token: UUID, entity_types: list[SyncEntityType]
    ) -> bool:
        """
        Delete specific checkpoints for a session.

        Args:
            session_token: The session token (UUID)
            entity_types: List of entity types to delete

        Returns:
            True if operation completed (even if no checkpoints existed)
        """
        if not entity_types:
            return True

        entity_type_values = [et.value for et in entity_types]
        await self._redis.hdel(_checkpoint_key(session_token), *entity_type_values)
        return True

    async def delete_all(self, session_token: UUID) -> bool:
        """
        Delete all checkpoints for a session.

        Args:
            session_token: The session token (UUID)

        Returns:
            True if operation completed (even if no checkpoints existed)
        """
        await self._redis.delete(_checkpoint_key(session_token))
        return True


async def get_checkpoint_store() -> CheckpointStore:
    """
    FastAPI dependency that provides a CheckpointStore instance.

    Returns:
        CheckpointStore configured with the singleton Redis client
    """
    redis_client = await get_redis_client()
    return CheckpointStore(redis_client)
