"""Unit tests for CheckpointStore with mocked Redis."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID

from routers.immich_models import SyncEntityType
from services.checkpoint_store import (
    Checkpoint,
    CheckpointDataError,
    CheckpointStore,
)

# Test UUIDs for consistent testing
TEST_SESSION_TOKEN = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_SESSION_TOKEN_2 = UUID("650e8400-e29b-41d4-a716-446655440001")


class TestCheckpointDataclass:
    """Tests for the Checkpoint dataclass."""

    def test_to_redis_value(self):
        """Test Checkpoint.to_redis_value() converts to pipe-delimited format."""
        last_synced = datetime(2025, 1, 20, 10, 30, 45, 123456, tzinfo=timezone.utc)
        updated = datetime(2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc)

        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AssetV1,
            last_synced_at=last_synced,
            updated_at=updated,
            last_entity_id="asset-123",
        )

        result = checkpoint.to_redis_value()

        assert (
            result
            == "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00|asset-123"
        )

    def test_to_redis_value_without_entity_id(self):
        """Test to_redis_value with no entity_id outputs empty string."""
        last_synced = datetime(2025, 1, 20, 10, 30, 45, 123456, tzinfo=timezone.utc)
        updated = datetime(2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc)

        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AssetV1,
            last_synced_at=last_synced,
            updated_at=updated,
            last_entity_id=None,
        )

        result = checkpoint.to_redis_value()

        assert result == "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00|"

    def test_from_redis_value(self):
        """Test Checkpoint.from_redis_value() parses pipe-delimited format."""
        value = "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00|asset-123"

        checkpoint = Checkpoint.from_redis_value(SyncEntityType.AssetV1, value)

        assert checkpoint.entity_type == SyncEntityType.AssetV1
        assert checkpoint.last_synced_at == datetime(
            2025, 1, 20, 10, 30, 45, 123456, tzinfo=timezone.utc
        )
        assert checkpoint.updated_at == datetime(
            2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc
        )
        assert checkpoint.last_entity_id == "asset-123"

    def test_from_redis_value_backward_compatible(self):
        """Test from_redis_value handles old 2-field format (backward compatible)."""
        value = "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00"

        checkpoint = Checkpoint.from_redis_value(SyncEntityType.AssetV1, value)

        assert checkpoint.entity_type == SyncEntityType.AssetV1
        assert checkpoint.last_synced_at == datetime(
            2025, 1, 20, 10, 30, 45, 123456, tzinfo=timezone.utc
        )
        assert checkpoint.last_entity_id is None

    def test_from_redis_value_empty_entity_id(self):
        """Test from_redis_value handles empty entity_id field."""
        value = "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00|"

        checkpoint = Checkpoint.from_redis_value(SyncEntityType.AssetV1, value)

        assert checkpoint.last_entity_id is None  # Empty string becomes None

    def test_from_redis_value_invalid_format_raises_error(self):
        """Test from_redis_value raises CheckpointDataError for invalid format."""
        # Missing pipe delimiter
        value = "2025-01-20T10:30:45.123456+00:00"

        with pytest.raises(CheckpointDataError) as exc_info:
            Checkpoint.from_redis_value(SyncEntityType.AssetV1, value)

        assert "invalid format" in str(exc_info.value)
        assert "AssetV1" in str(exc_info.value)

    def test_from_redis_value_too_many_parts_raises_error(self):
        """Test from_redis_value raises CheckpointDataError for too many parts."""
        value = "2025-01-20T10:30:45+00:00|2025-01-20T10:30:45+00:00|entity|extra"

        with pytest.raises(CheckpointDataError) as exc_info:
            Checkpoint.from_redis_value(SyncEntityType.AssetV1, value)

        assert "invalid format" in str(exc_info.value)

    def test_from_redis_value_invalid_timestamp_raises_error(self):
        """Test from_redis_value raises CheckpointDataError for invalid timestamp."""
        value = "not-a-timestamp|2025-01-20T10:30:45+00:00|entity-123"

        with pytest.raises(CheckpointDataError) as exc_info:
            Checkpoint.from_redis_value(SyncEntityType.AssetV1, value)

        assert "invalid timestamp" in str(exc_info.value)
        assert "AssetV1" in str(exc_info.value)

    def test_roundtrip_conversion(self):
        """Test that to_redis_value and from_redis_value are inverses."""
        original = Checkpoint(
            entity_type=SyncEntityType.PersonV1,
            last_synced_at=datetime(2025, 1, 19, 14, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2025, 1, 19, 14, 0, 0, tzinfo=timezone.utc),
            last_entity_id="person-456",
        )

        redis_value = original.to_redis_value()
        restored = Checkpoint.from_redis_value(SyncEntityType.PersonV1, redis_value)

        assert restored.entity_type == original.entity_type
        assert restored.last_synced_at == original.last_synced_at
        assert restored.updated_at == original.updated_at
        assert restored.last_entity_id == original.last_entity_id


class TestCheckpointStoreGetAll:
    """Tests for CheckpointStore.get_all()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        return AsyncMock()

    @pytest.fixture
    def checkpoint_store(self, mock_redis):
        """Create CheckpointStore with mocked Redis."""
        return CheckpointStore(mock_redis)

    @pytest.mark.anyio
    async def test_get_all_returns_checkpoints(self, checkpoint_store, mock_redis):
        """Test getting all checkpoints for a session."""
        mock_redis.hgetall.return_value = {
            "AssetV1": "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00|asset-123",
            "AlbumV1": "2025-01-20T09:30:00.000000+00:00|2025-01-20T09:30:00+00:00|album-456",
        }

        checkpoints = await checkpoint_store.get_all(TEST_SESSION_TOKEN)

        assert len(checkpoints) == 2
        entity_types = {c.entity_type for c in checkpoints}
        assert entity_types == {SyncEntityType.AssetV1, SyncEntityType.AlbumV1}

    @pytest.mark.anyio
    async def test_get_all_returns_empty_list_when_no_checkpoints(
        self, checkpoint_store, mock_redis
    ):
        """Test getting checkpoints when none exist."""
        mock_redis.hgetall.return_value = {}

        checkpoints = await checkpoint_store.get_all(TEST_SESSION_TOKEN)

        assert checkpoints == []

    @pytest.mark.anyio
    async def test_get_all_raises_on_malformed_checkpoints(
        self, checkpoint_store, mock_redis
    ):
        """Test get_all raises CheckpointDataError for malformed checkpoint values."""
        mock_redis.hgetall.return_value = {
            "AssetV1": "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00|asset-123",
            "AlbumV1": "malformed-data",  # Invalid format
        }

        with pytest.raises(CheckpointDataError):
            await checkpoint_store.get_all(TEST_SESSION_TOKEN)

    @pytest.mark.anyio
    async def test_get_all_raises_on_unknown_entity_types(
        self, checkpoint_store, mock_redis
    ):
        """Test get_all raises ValueError for unknown entity types."""
        mock_redis.hgetall.return_value = {
            "AssetV1": "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00|asset-123",
            "UnknownTypeV1": "2025-01-20T09:30:00.000000+00:00|2025-01-20T09:30:00+00:00|unknown-123",
        }

        with pytest.raises(ValueError, match="UnknownTypeV1"):
            await checkpoint_store.get_all(TEST_SESSION_TOKEN)


class TestCheckpointStoreGet:
    """Tests for CheckpointStore.get()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        return AsyncMock()

    @pytest.fixture
    def checkpoint_store(self, mock_redis):
        """Create CheckpointStore with mocked Redis."""
        return CheckpointStore(mock_redis)

    @pytest.mark.anyio
    async def test_get_returns_checkpoint(self, checkpoint_store, mock_redis):
        """Test getting a specific checkpoint."""
        mock_redis.hget.return_value = (
            "2025-01-20T10:30:45.123456+00:00|2025-01-20T10:30:45+00:00|asset-123"
        )

        checkpoint = await checkpoint_store.get(
            TEST_SESSION_TOKEN, SyncEntityType.AssetV1
        )

        assert checkpoint is not None
        assert checkpoint.entity_type == SyncEntityType.AssetV1
        assert checkpoint.last_synced_at == datetime(
            2025, 1, 20, 10, 30, 45, 123456, tzinfo=timezone.utc
        )
        assert checkpoint.last_entity_id == "asset-123"

    @pytest.mark.anyio
    async def test_get_returns_none_when_not_found(self, checkpoint_store, mock_redis):
        """Test get returns None when checkpoint doesn't exist."""
        mock_redis.hget.return_value = None

        checkpoint = await checkpoint_store.get(
            TEST_SESSION_TOKEN, SyncEntityType.AssetV1
        )

        assert checkpoint is None


class TestCheckpointStoreSet:
    """Tests for CheckpointStore.set()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        return AsyncMock()

    @pytest.fixture
    def checkpoint_store(self, mock_redis):
        """Create CheckpointStore with mocked Redis."""
        return CheckpointStore(mock_redis)

    @pytest.mark.anyio
    async def test_set_stores_checkpoint(self, checkpoint_store, mock_redis):
        """Test setting a checkpoint."""
        last_synced_at = datetime(2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc)

        result = await checkpoint_store.set(
            TEST_SESSION_TOKEN, SyncEntityType.AssetV1, last_synced_at, "asset-123"
        )

        assert result is True
        mock_redis.hset.assert_called_once()

        # Verify the call arguments
        call_args = mock_redis.hset.call_args
        assert call_args[0][0] == f"session:{TEST_SESSION_TOKEN}:checkpoints"
        assert call_args[0][1] == "AssetV1"
        # Value should be pipe-delimited with last_synced_at, updated_at, and entity_id
        value = call_args[0][2]
        assert value.startswith("2025-01-20T10:30:45+00:00|")
        assert "|asset-123" in value


class TestCheckpointStoreSetMany:
    """Tests for CheckpointStore.set_many()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        return AsyncMock()

    @pytest.fixture
    def checkpoint_store(self, mock_redis):
        """Create CheckpointStore with mocked Redis."""
        return CheckpointStore(mock_redis)

    @pytest.mark.anyio
    async def test_set_many_stores_multiple_checkpoints(
        self, checkpoint_store, mock_redis
    ):
        """Test setting multiple checkpoints atomically."""
        checkpoints = [
            (
                SyncEntityType.AssetV1,
                datetime(2025, 1, 20, 10, 30, 45, tzinfo=timezone.utc),
                "asset-123",
            ),
            (
                SyncEntityType.AlbumV1,
                datetime(2025, 1, 20, 9, 30, 0, tzinfo=timezone.utc),
                "album-456",
            ),
        ]

        result = await checkpoint_store.set_many(TEST_SESSION_TOKEN, checkpoints)

        assert result is True
        mock_redis.hset.assert_called_once()

        # Verify the mapping was passed
        call_args = mock_redis.hset.call_args
        assert call_args[0][0] == f"session:{TEST_SESSION_TOKEN}:checkpoints"
        assert "mapping" in call_args[1]
        mapping = call_args[1]["mapping"]
        assert "AssetV1" in mapping
        assert "AlbumV1" in mapping

    @pytest.mark.anyio
    async def test_set_many_returns_true_for_empty_list(
        self, checkpoint_store, mock_redis
    ):
        """Test set_many returns True for empty checkpoint list."""
        result = await checkpoint_store.set_many(TEST_SESSION_TOKEN, [])

        assert result is True
        mock_redis.hset.assert_not_called()


class TestCheckpointStoreDelete:
    """Tests for CheckpointStore.delete()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        return AsyncMock()

    @pytest.fixture
    def checkpoint_store(self, mock_redis):
        """Create CheckpointStore with mocked Redis."""
        return CheckpointStore(mock_redis)

    @pytest.mark.anyio
    async def test_delete_removes_specified_checkpoints(
        self, checkpoint_store, mock_redis
    ):
        """Test deleting specific checkpoints."""
        result = await checkpoint_store.delete(
            TEST_SESSION_TOKEN, [SyncEntityType.AssetV1, SyncEntityType.AlbumV1]
        )

        assert result is True
        mock_redis.hdel.assert_called_once_with(
            f"session:{TEST_SESSION_TOKEN}:checkpoints", "AssetV1", "AlbumV1"
        )

    @pytest.mark.anyio
    async def test_delete_returns_true_for_empty_list(
        self, checkpoint_store, mock_redis
    ):
        """Test delete returns True for empty entity type list."""
        result = await checkpoint_store.delete(TEST_SESSION_TOKEN, [])

        assert result is True
        mock_redis.hdel.assert_not_called()


class TestCheckpointStoreDeleteAll:
    """Tests for CheckpointStore.delete_all()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        return AsyncMock()

    @pytest.fixture
    def checkpoint_store(self, mock_redis):
        """Create CheckpointStore with mocked Redis."""
        return CheckpointStore(mock_redis)

    @pytest.mark.anyio
    async def test_delete_all_removes_checkpoint_key(
        self, checkpoint_store, mock_redis
    ):
        """Test delete_all removes the entire checkpoint hash."""
        result = await checkpoint_store.delete_all(TEST_SESSION_TOKEN)

        assert result is True
        mock_redis.delete.assert_called_once_with(
            f"session:{TEST_SESSION_TOKEN}:checkpoints"
        )
