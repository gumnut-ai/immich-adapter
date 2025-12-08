"""Unit tests for SessionStore with mocked Redis."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

from services.session_store import (
    Session,
    SessionDataError,
    SessionExpiredError,
    SessionStore,
)

# Test UUID for consistent testing
TEST_IMMICH_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_IMMICH_ID_2 = UUID("650e8400-e29b-41d4-a716-446655440001")


class TestSessionDataclass:
    """Tests for the Session dataclass."""

    def test_to_dict(self):
        """Test Session.to_dict() converts to Redis hash format."""
        now = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        session = Session(
            id="abc123",
            immich_id=TEST_IMMICH_ID,
            user_id="user_123",
            library_id="lib_456",
            device_type="iOS",
            device_os="iOS 17.4",
            app_version="1.94.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )

        result = session.to_dict()

        assert result["immich_id"] == str(TEST_IMMICH_ID)
        assert result["user_id"] == "user_123"
        assert result["library_id"] == "lib_456"
        assert result["device_type"] == "iOS"
        assert result["device_os"] == "iOS 17.4"
        assert result["app_version"] == "1.94.0"
        assert result["created_at"] == "2025-01-20T10:00:00+00:00"
        assert result["updated_at"] == "2025-01-20T10:00:00+00:00"
        assert result["is_pending_sync_reset"] == "0"

    def test_to_dict_with_pending_sync_reset(self):
        """Test to_dict with is_pending_sync_reset=True."""
        now = datetime.now(timezone.utc)
        session = Session(
            id="abc123",
            immich_id=TEST_IMMICH_ID,
            user_id="user_123",
            library_id="lib_456",
            device_type="iOS",
            device_os="iOS 17.4",
            app_version="1.94.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=True,
        )

        result = session.to_dict()

        assert result["is_pending_sync_reset"] == "1"

    def test_from_dict(self):
        """Test Session.from_dict() creates Session from Redis hash data."""
        data = {
            "immich_id": str(TEST_IMMICH_ID),
            "user_id": "user_123",
            "library_id": "lib_456",
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        session = Session.from_dict("abc123", data)

        assert session.id == "abc123"
        assert session.immich_id == TEST_IMMICH_ID
        assert session.user_id == "user_123"
        assert session.library_id == "lib_456"
        assert session.device_type == "iOS"
        assert session.device_os == "iOS 17.4"
        assert session.app_version == "1.94.0"
        assert session.created_at == datetime(
            2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc
        )
        assert session.updated_at == datetime(
            2025, 1, 20, 10, 30, 0, tzinfo=timezone.utc
        )
        assert session.is_pending_sync_reset is False

    def test_from_dict_with_pending_sync_reset(self):
        """Test from_dict with is_pending_sync_reset='1'."""
        data = {
            "immich_id": str(TEST_IMMICH_ID),
            "user_id": "user_123",
            "library_id": "lib_456",
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "1",
        }

        session = Session.from_dict("abc123", data)

        assert session.is_pending_sync_reset is True

    def test_from_dict_missing_fields_raises_error(self):
        """Test from_dict raises SessionDataError when fields are missing."""
        data = {
            "user_id": "user_123",
            # Missing all other required fields
        }

        with pytest.raises(SessionDataError) as exc_info:
            Session.from_dict("abc123", data)

        assert "missing required fields" in str(exc_info.value)
        assert "abc123" in str(exc_info.value)

    def test_from_dict_malformed_datetime_raises_error(self):
        """Test from_dict raises SessionDataError for invalid datetime."""
        data = {
            "immich_id": str(TEST_IMMICH_ID),
            "user_id": "user_123",
            "library_id": "lib_456",
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "not-a-valid-datetime",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        with pytest.raises(SessionDataError) as exc_info:
            Session.from_dict("abc123", data)

        assert "malformed data" in str(exc_info.value)
        assert "abc123" in str(exc_info.value)


class TestHashJwt:
    """Tests for SessionStore.hash_jwt()."""

    def test_hash_jwt_returns_sha256_hex(self):
        """Test hash_jwt returns a 64-character hex string (SHA-256)."""
        jwt = "test.jwt.token"
        result = SessionStore.hash_jwt(jwt)

        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_hash_jwt_is_consistent(self):
        """Test hash_jwt returns same hash for same input."""
        jwt = "test.jwt.token"
        result1 = SessionStore.hash_jwt(jwt)
        result2 = SessionStore.hash_jwt(jwt)

        assert result1 == result2

    def test_hash_jwt_different_tokens_different_hashes(self):
        """Test different JWTs produce different hashes."""
        jwt1 = "test.jwt.token1"
        jwt2 = "test.jwt.token2"

        result1 = SessionStore.hash_jwt(jwt1)
        result2 = SessionStore.hash_jwt(jwt2)

        assert result1 != result2


class TestSessionStoreCreate:
    """Tests for SessionStore.create()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        mock = AsyncMock()
        # pipeline() is sync, but execute() is async
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock()
        mock.pipeline = MagicMock(return_value=mock_pipeline)
        return mock

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_create_session(self, session_store, mock_redis):
        """Test creating a new session."""
        session = await session_store.create(
            jwt_token="test.jwt",
            user_id="user_123",
            library_id="lib_456",
            device_type="iOS",
            device_os="iOS 17.4",
            app_version="1.94.0",
        )

        assert session.user_id == "user_123"
        assert session.library_id == "lib_456"
        assert session.device_type == "iOS"
        assert session.device_os == "iOS 17.4"
        assert session.app_version == "1.94.0"
        assert session.is_pending_sync_reset is False
        assert session.id == SessionStore.hash_jwt("test.jwt")
        # immich_id should be a valid UUID (generated at creation)
        assert session.immich_id is not None
        assert isinstance(session.immich_id, UUID)

        # Verify pipeline was executed
        mock_redis.pipeline.return_value.execute.assert_called_once()

    @pytest.mark.anyio
    async def test_create_session_with_expiry(self, session_store, mock_redis):
        """Test creating a session with TTL."""
        # Set expiry to 1 hour in the future
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        await session_store.create(
            jwt_token="test.jwt",
            user_id="user_123",
            library_id="lib_456",
            device_type="iOS",
            device_os="iOS 17.4",
            app_version="1.94.0",
            expires_at=expires_at,
        )

        # Verify expire was called on pipeline
        mock_pipeline = mock_redis.pipeline.return_value
        mock_pipeline.expire.assert_called()

    @pytest.mark.anyio
    async def test_create_session_with_past_expiry_raises_error(
        self, session_store, mock_redis
    ):
        """Test creating a session with past expiration raises SessionExpiredError."""
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)

        with pytest.raises(SessionExpiredError) as exc_info:
            await session_store.create(
                jwt_token="test.jwt",
                user_id="user_123",
                library_id="lib_456",
                device_type="iOS",
                device_os="iOS 17.4",
                app_version="1.94.0",
                expires_at=past_time,
            )

        assert "in the past" in str(exc_info.value)

    @pytest.mark.anyio
    async def test_create_session_verifies_pipeline_commands(
        self, session_store, mock_redis
    ):
        """Test that create issues correct pipeline commands."""
        session = await session_store.create(
            jwt_token="test.jwt",
            user_id="user_123",
            library_id="lib_456",
            device_type="iOS",
            device_os="iOS 17.4",
            app_version="1.94.0",
        )

        mock_pipeline = mock_redis.pipeline.return_value

        # Verify hset was called with session data
        mock_pipeline.hset.assert_called()
        hset_call = mock_pipeline.hset.call_args
        assert f"session:{session.id}" == hset_call[0][0]

        # Verify sadd was called to add to user's session set
        mock_pipeline.sadd.assert_called_with(
            f"user:{session.user_id}:sessions", session.id
        )

        # Verify zadd was called for the activity index
        mock_pipeline.zadd.assert_called()


class TestSessionStoreGet:
    """Tests for SessionStore.get() and get_by_id()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        return AsyncMock()

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_get_session_found(self, session_store, mock_redis):
        """Test getting an existing session."""
        mock_redis.hgetall.return_value = {
            "immich_id": str(TEST_IMMICH_ID),
            "user_id": "user_123",
            "library_id": "lib_456",
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        session = await session_store.get("test.jwt")

        assert session is not None
        assert session.user_id == "user_123"
        assert session.library_id == "lib_456"
        assert session.immich_id == TEST_IMMICH_ID

    @pytest.mark.anyio
    async def test_get_session_not_found(self, session_store, mock_redis):
        """Test getting a non-existent session."""
        mock_redis.hgetall.return_value = {}

        session = await session_store.get("nonexistent.jwt")

        assert session is None

    @pytest.mark.anyio
    async def test_get_by_id(self, session_store, mock_redis):
        """Test getting session by ID."""
        mock_redis.hgetall.return_value = {
            "immich_id": str(TEST_IMMICH_ID),
            "user_id": "user_123",
            "library_id": "lib_456",
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        session = await session_store.get_by_id("session_id_123")

        assert session is not None
        assert session.id == "session_id_123"
        assert session.immich_id == TEST_IMMICH_ID


class TestSessionStoreGetByUser:
    """Tests for SessionStore.get_by_user()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client with pipeline support."""
        mock = AsyncMock()
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock()
        mock.pipeline = MagicMock(return_value=mock_pipeline)
        return mock

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_get_by_user_with_sessions(self, session_store, mock_redis):
        """Test getting all sessions for a user using pipeline."""
        mock_redis.smembers.return_value = {"session_1", "session_2"}

        # Pipeline returns list of hgetall results
        mock_pipeline = mock_redis.pipeline.return_value
        mock_pipeline.execute.return_value = [
            {
                "immich_id": str(TEST_IMMICH_ID),
                "user_id": "user_123",
                "library_id": "lib_456",
                "device_type": "iOS",
                "device_os": "iOS 17.4",
                "app_version": "1.94.0",
                "created_at": "2025-01-20T10:00:00+00:00",
                "updated_at": "2025-01-20T10:30:00+00:00",
                "is_pending_sync_reset": "0",
            },
            {
                "immich_id": str(TEST_IMMICH_ID_2),
                "user_id": "user_123",
                "library_id": "lib_456",
                "device_type": "Chrome",
                "device_os": "macOS 14",
                "app_version": "",
                "created_at": "2025-01-20T11:00:00+00:00",
                "updated_at": "2025-01-20T11:30:00+00:00",
                "is_pending_sync_reset": "0",
            },
        ]

        sessions = await session_store.get_by_user("user_123")

        assert len(sessions) == 2
        # Verify pipeline was used
        mock_pipeline.hgetall.assert_called()

    @pytest.mark.anyio
    async def test_get_by_user_no_sessions(self, session_store, mock_redis):
        """Test getting sessions for user with no sessions."""
        mock_redis.smembers.return_value = set()

        sessions = await session_store.get_by_user("user_123")

        assert sessions == []

    @pytest.mark.anyio
    async def test_get_by_user_cleans_up_orphaned_sessions(
        self, session_store, mock_redis
    ):
        """Test that get_by_user cleans up orphaned index entries."""
        mock_redis.smembers.return_value = {"session_1", "orphaned_session"}

        # First pipeline for fetching - one valid session, one expired (empty dict)
        fetch_pipeline = MagicMock()
        fetch_pipeline.execute = AsyncMock(
            return_value=[
                {
                    "immich_id": str(TEST_IMMICH_ID),
                    "user_id": "user_123",
                    "library_id": "lib_456",
                    "device_type": "iOS",
                    "device_os": "iOS 17.4",
                    "app_version": "1.94.0",
                    "created_at": "2025-01-20T10:00:00+00:00",
                    "updated_at": "2025-01-20T10:30:00+00:00",
                    "is_pending_sync_reset": "0",
                },
                {},  # Orphaned session (expired via TTL)
            ]
        )

        # Second pipeline for cleanup
        cleanup_pipeline = MagicMock()
        cleanup_pipeline.execute = AsyncMock(return_value=[])

        mock_redis.pipeline = MagicMock(side_effect=[fetch_pipeline, cleanup_pipeline])

        sessions = await session_store.get_by_user("user_123")

        # Should only return the valid session
        assert len(sessions) == 1
        assert sessions[0].device_type == "iOS"

        # Cleanup pipeline should have been called
        cleanup_pipeline.srem.assert_called()
        cleanup_pipeline.zrem.assert_called()


class TestSessionStoreGetByImmichId:
    """Tests for SessionStore.get_by_immich_id()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client with pipeline support."""
        mock = AsyncMock()
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock()
        mock.pipeline = MagicMock(return_value=mock_pipeline)
        return mock

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_get_by_immich_id_found(self, session_store, mock_redis):
        """Test finding a session by user ID and Immich ID."""
        mock_redis.smembers.return_value = {"session_1", "session_2"}

        mock_pipeline = mock_redis.pipeline.return_value
        mock_pipeline.execute.return_value = [
            {
                "immich_id": str(TEST_IMMICH_ID),
                "user_id": "user_123",
                "library_id": "lib_456",
                "device_type": "iOS",
                "device_os": "iOS 17.4",
                "app_version": "1.94.0",
                "created_at": "2025-01-20T10:00:00+00:00",
                "updated_at": "2025-01-20T10:30:00+00:00",
                "is_pending_sync_reset": "0",
            },
            {
                "immich_id": str(TEST_IMMICH_ID_2),
                "user_id": "user_123",
                "library_id": "lib_456",
                "device_type": "Chrome",
                "device_os": "macOS 14",
                "app_version": "",
                "created_at": "2025-01-20T11:00:00+00:00",
                "updated_at": "2025-01-20T11:30:00+00:00",
                "is_pending_sync_reset": "0",
            },
        ]

        session = await session_store.get_by_immich_id("user_123", TEST_IMMICH_ID)

        assert session is not None
        assert session.immich_id == TEST_IMMICH_ID
        assert session.device_type == "iOS"

    @pytest.mark.anyio
    async def test_get_by_immich_id_not_found(self, session_store, mock_redis):
        """Test getting session by Immich ID when not found."""
        mock_redis.smembers.return_value = {"session_1"}

        mock_pipeline = mock_redis.pipeline.return_value
        mock_pipeline.execute.return_value = [
            {
                "immich_id": str(TEST_IMMICH_ID),
                "user_id": "user_123",
                "library_id": "lib_456",
                "device_type": "iOS",
                "device_os": "iOS 17.4",
                "app_version": "1.94.0",
                "created_at": "2025-01-20T10:00:00+00:00",
                "updated_at": "2025-01-20T10:30:00+00:00",
                "is_pending_sync_reset": "0",
            },
        ]

        # Search for a different Immich ID
        session = await session_store.get_by_immich_id("user_123", TEST_IMMICH_ID_2)

        assert session is None

    @pytest.mark.anyio
    async def test_get_by_immich_id_no_sessions(self, session_store, mock_redis):
        """Test getting session by Immich ID when user has no sessions."""
        mock_redis.smembers.return_value = set()

        session = await session_store.get_by_immich_id("user_123", TEST_IMMICH_ID)

        assert session is None


class TestSessionStoreDelete:
    """Tests for SessionStore.delete() and delete_by_id()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        mock = AsyncMock()
        # pipeline() is sync, but execute() is async
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock()
        mock.pipeline = MagicMock(return_value=mock_pipeline)
        return mock

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_delete_session_exists(self, session_store, mock_redis):
        """Test deleting an existing session."""
        mock_redis.hgetall.return_value = {
            "immich_id": str(TEST_IMMICH_ID),
            "user_id": "user_123",
            "library_id": "lib_456",
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        result = await session_store.delete("test.jwt")

        assert result is True
        mock_redis.pipeline.return_value.execute.assert_called_once()

    @pytest.mark.anyio
    async def test_delete_session_not_found(self, session_store, mock_redis):
        """Test deleting a non-existent session."""
        mock_redis.hgetall.return_value = {}

        result = await session_store.delete("nonexistent.jwt")

        assert result is False

    @pytest.mark.anyio
    async def test_delete_verifies_pipeline_commands(self, session_store, mock_redis):
        """Test that delete issues correct pipeline commands without checkpoint."""
        session_id = SessionStore.hash_jwt("test.jwt")
        mock_redis.hgetall.return_value = {
            "immich_id": str(TEST_IMMICH_ID),
            "user_id": "user_123",
            "library_id": "lib_456",
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        await session_store.delete("test.jwt")

        mock_pipeline = mock_redis.pipeline.return_value

        # Verify delete was called for session
        mock_pipeline.delete.assert_called_once_with(f"session:{session_id}")

        # Verify srem was called to remove from user's session set
        mock_pipeline.srem.assert_called_with("user:user_123:sessions", session_id)

        # Verify zrem was called for the activity index
        mock_pipeline.zrem.assert_called_with("sessions:by_updated_at", session_id)


class TestSessionStoreUpdateActivity:
    """Tests for SessionStore.update_activity()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        mock = AsyncMock()
        # pipeline() is sync, but execute() is async
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock()
        mock.pipeline = MagicMock(return_value=mock_pipeline)
        return mock

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_update_activity_session_exists(self, session_store, mock_redis):
        """Test updating activity for existing session."""
        mock_redis.exists.return_value = True

        result = await session_store.update_activity("test.jwt")

        assert result is True
        mock_redis.pipeline.return_value.execute.assert_called_once()

    @pytest.mark.anyio
    async def test_update_activity_session_not_found(self, session_store, mock_redis):
        """Test updating activity for non-existent session."""
        mock_redis.exists.return_value = False

        result = await session_store.update_activity("nonexistent.jwt")

        assert result is False


class TestSessionStoreSyncReset:
    """Tests for SessionStore.set_pending_sync_reset()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        return AsyncMock()

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_set_pending_sync_reset_true(self, session_store, mock_redis):
        """Test setting sync reset flag to true."""
        mock_redis.exists.return_value = True

        result = await session_store.set_pending_sync_reset("session_123", True)

        assert result is True
        mock_redis.hset.assert_called_with(
            "session:session_123", "is_pending_sync_reset", "1"
        )

    @pytest.mark.anyio
    async def test_set_pending_sync_reset_false(self, session_store, mock_redis):
        """Test setting sync reset flag to false."""
        mock_redis.exists.return_value = True

        result = await session_store.set_pending_sync_reset("session_123", False)

        assert result is True
        mock_redis.hset.assert_called_with(
            "session:session_123", "is_pending_sync_reset", "0"
        )

    @pytest.mark.anyio
    async def test_set_pending_sync_reset_session_not_found(
        self, session_store, mock_redis
    ):
        """Test setting sync reset for non-existent session."""
        mock_redis.exists.return_value = False

        result = await session_store.set_pending_sync_reset("nonexistent", True)

        assert result is False


class TestSessionStoreExists:
    """Tests for SessionStore.exists()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        return AsyncMock()

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_exists_true(self, session_store, mock_redis):
        """Test exists returns True for existing session."""
        mock_redis.exists.return_value = 1

        result = await session_store.exists("test.jwt")

        assert result is True

    @pytest.mark.anyio
    async def test_exists_false(self, session_store, mock_redis):
        """Test exists returns False for non-existent session."""
        mock_redis.exists.return_value = 0

        result = await session_store.exists("nonexistent.jwt")

        assert result is False


class TestSessionStoreGetTtl:
    """Tests for SessionStore.get_ttl()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        return AsyncMock()

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_get_ttl_with_ttl(self, session_store, mock_redis):
        """Test get_ttl returns seconds remaining."""
        mock_redis.ttl.return_value = 3600

        result = await session_store.get_ttl("test.jwt")

        assert result == 3600

    @pytest.mark.anyio
    async def test_get_ttl_no_ttl_set(self, session_store, mock_redis):
        """Test get_ttl returns None when no TTL set."""
        mock_redis.ttl.return_value = -1

        result = await session_store.get_ttl("test.jwt")

        assert result is None

    @pytest.mark.anyio
    async def test_get_ttl_key_not_found(self, session_store, mock_redis):
        """Test get_ttl returns None when key doesn't exist."""
        mock_redis.ttl.return_value = -2

        result = await session_store.get_ttl("nonexistent.jwt")

        assert result is None


class TestSessionStoreDeleteAllForUser:
    """Tests for SessionStore.delete_all_for_user()."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        mock = AsyncMock()
        # pipeline() is sync, but execute() is async
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock()
        mock.pipeline = MagicMock(return_value=mock_pipeline)
        return mock

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_delete_all_for_user(self, session_store, mock_redis):
        """Test deleting all sessions for a user using pipeline."""
        mock_redis.smembers.return_value = {"session_1", "session_2"}

        count = await session_store.delete_all_for_user("user_123")

        assert count == 2

        # Verify pipeline commands
        mock_pipeline = mock_redis.pipeline.return_value
        assert mock_pipeline.delete.call_count >= 2  # sessions + user set
        mock_pipeline.zrem.assert_called()
        mock_pipeline.execute.assert_called_once()

    @pytest.mark.anyio
    async def test_delete_all_for_user_no_sessions(self, session_store, mock_redis):
        """Test deleting sessions for user with no sessions."""
        mock_redis.smembers.return_value = set()

        count = await session_store.delete_all_for_user("user_123")

        assert count == 0


class TestSessionStoreStaleCleanup:
    """Tests for stale session cleanup methods."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client."""
        mock = AsyncMock()
        # pipeline() is sync, but execute() is async
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock()
        mock.pipeline = MagicMock(return_value=mock_pipeline)
        return mock

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_get_stale_sessions(self, session_store, mock_redis):
        """Test getting stale session IDs."""
        mock_redis.zrangebyscore.return_value = ["session_1", "session_2"]

        stale_ids = await session_store.get_stale_sessions(days=90)

        assert len(stale_ids) == 2
        assert "session_1" in stale_ids
        assert "session_2" in stale_ids

    @pytest.mark.anyio
    async def test_cleanup_stale_sessions(self, session_store, mock_redis):
        """Test cleaning up stale sessions using pipeline."""
        mock_redis.zrangebyscore.return_value = ["session_1"]

        # First pipeline for fetching session data
        fetch_pipeline = MagicMock()
        fetch_pipeline.execute = AsyncMock(
            return_value=[
                {
                    "immich_id": str(TEST_IMMICH_ID),
                    "user_id": "user_123",
                    "library_id": "lib_456",
                    "device_type": "iOS",
                    "device_os": "iOS 17.4",
                    "app_version": "1.94.0",
                    "created_at": "2025-01-20T10:00:00+00:00",
                    "updated_at": "2025-01-20T10:30:00+00:00",
                    "is_pending_sync_reset": "0",
                }
            ]
        )

        # Second pipeline for deletion
        delete_pipeline = MagicMock()
        delete_pipeline.execute = AsyncMock(return_value=[])

        mock_redis.pipeline = MagicMock(side_effect=[fetch_pipeline, delete_pipeline])

        count = await session_store.cleanup_stale_sessions(days=90)

        assert count == 1
        delete_pipeline.delete.assert_called()
        delete_pipeline.srem.assert_called()
        delete_pipeline.zrem.assert_called()

    @pytest.mark.anyio
    async def test_cleanup_stale_sessions_handles_already_expired(
        self, session_store, mock_redis
    ):
        """Test cleanup handles sessions that expired via TTL."""
        mock_redis.zrangebyscore.return_value = ["session_1", "already_expired"]

        # First pipeline - one valid, one already expired (empty)
        fetch_pipeline = MagicMock()
        fetch_pipeline.execute = AsyncMock(
            return_value=[
                {
                    "immich_id": str(TEST_IMMICH_ID),
                    "user_id": "user_123",
                    "library_id": "lib_456",
                    "device_type": "iOS",
                    "device_os": "iOS 17.4",
                    "app_version": "1.94.0",
                    "created_at": "2025-01-20T10:00:00+00:00",
                    "updated_at": "2025-01-20T10:30:00+00:00",
                    "is_pending_sync_reset": "0",
                },
                {},  # Already expired
            ]
        )

        # Second pipeline for deletion
        delete_pipeline = MagicMock()
        delete_pipeline.execute = AsyncMock(return_value=[])

        mock_redis.pipeline = MagicMock(side_effect=[fetch_pipeline, delete_pipeline])

        count = await session_store.cleanup_stale_sessions(days=90)

        # Only the valid session should be counted
        assert count == 1

        # But zrem should still be called for the already-expired one
        assert delete_pipeline.zrem.call_count >= 1

    @pytest.mark.anyio
    async def test_cleanup_stale_sessions_no_stale(self, session_store, mock_redis):
        """Test cleanup with no stale sessions."""
        mock_redis.zrangebyscore.return_value = []

        count = await session_store.cleanup_stale_sessions(days=90)

        assert count == 0
