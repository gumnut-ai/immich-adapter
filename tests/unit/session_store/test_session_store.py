"""Unit tests for SessionStore with mocked Redis."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import redis.exceptions

from services.session_store import (
    _REQUIRED_SESSION_FIELDS,
    _SET_LIBRARY_IF_LUA,
    _UPDATE_IF_EXISTS_LUA,
    Session,
    SessionDataError,
    SessionExpiredError,
    SessionStore,
    SessionStoreError,
)

# Test UUIDs for consistent testing
TEST_SESSION_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_SESSION_ID_2 = UUID("650e8400-e29b-41d4-a716-446655440001")
TEST_ENCRYPTED_JWT = "gAAAAABh..."  # Mock encrypted JWT

# Every writer that conditionally updates the session hash.
WRITERS = [
    pytest.param(
        lambda store, token: store.update_activity(token), id="update_activity"
    ),
    pytest.param(
        lambda store, token: store.update_stored_jwt(token, "new.jwt.token"),
        id="update_stored_jwt",
    ),
    pytest.param(lambda store, token: store.forget_library(token), id="forget_library"),
    pytest.param(
        lambda store, token: store.set_pending_sync_reset(token, True),
        id="set_pending_sync_reset",
    ),
]


class TestSessionDataclass:
    """Tests for the Session dataclass."""

    def test_to_dict(self):
        """Test Session.to_dict() converts to Redis hash format."""
        now = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        session = Session(
            id=TEST_SESSION_ID,
            user_id="user_123",
            library_id="lib_456",
            stored_jwt=TEST_ENCRYPTED_JWT,
            device_type="iOS",
            device_os="iOS 17.4",
            app_version="1.94.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )

        result = session.to_dict()

        assert result["user_id"] == "user_123"
        assert result["library_id"] == "lib_456"
        assert result["stored_jwt"] == TEST_ENCRYPTED_JWT
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
            id=TEST_SESSION_ID,
            user_id="user_123",
            library_id="lib_456",
            stored_jwt=TEST_ENCRYPTED_JWT,
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
            "user_id": "user_123",
            "library_id": "lib_456",
            "stored_jwt": TEST_ENCRYPTED_JWT,
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        session = Session.from_dict(TEST_SESSION_ID, data)

        assert session.id == TEST_SESSION_ID
        assert session.user_id == "user_123"
        assert session.library_id == "lib_456"
        assert session.stored_jwt == TEST_ENCRYPTED_JWT
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
        # A hash written before library revalidation existed is due for it.
        assert session.library_checked_at == 0.0
        assert session.library_from_choice is False

    def test_library_revalidation_fields_round_trip(self):
        now = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        session = Session(
            id=TEST_SESSION_ID,
            user_id="user_123",
            library_id="lib_456",
            stored_jwt=TEST_ENCRYPTED_JWT,
            device_type="iOS",
            device_os="iOS 17.4",
            app_version="1.94.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
            library_checked_at=1700000000.5,
            library_from_choice=True,
        )

        restored = Session.from_dict(TEST_SESSION_ID, session.to_dict())

        assert restored.library_checked_at == 1700000000.5
        assert restored.library_from_choice is True

    def test_from_dict_with_pending_sync_reset(self):
        """Test from_dict with is_pending_sync_reset='1'."""
        data = {
            "user_id": "user_123",
            "library_id": "lib_456",
            "stored_jwt": TEST_ENCRYPTED_JWT,
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "1",
        }

        session = Session.from_dict(TEST_SESSION_ID, data)

        assert session.is_pending_sync_reset is True

    def test_from_dict_missing_fields_raises_error(self):
        """Test from_dict raises SessionDataError when fields are missing."""
        data = {
            "user_id": "user_123",
            # Missing all other required fields
        }

        with pytest.raises(SessionDataError) as exc_info:
            Session.from_dict(TEST_SESSION_ID, data)

        assert "missing required fields" in str(exc_info.value)
        assert str(TEST_SESSION_ID) in str(exc_info.value)

    def test_from_dict_malformed_datetime_raises_error(self):
        """Test from_dict raises SessionDataError for invalid datetime."""
        data = {
            "user_id": "user_123",
            "library_id": "lib_456",
            "stored_jwt": TEST_ENCRYPTED_JWT,
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "not-a-valid-datetime",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        with pytest.raises(SessionDataError) as exc_info:
            Session.from_dict(TEST_SESSION_ID, data)

        assert "malformed data" in str(exc_info.value)
        assert str(TEST_SESSION_ID) in str(exc_info.value)

    def test_get_jwt_decrypts_stored_jwt(self):
        """Test get_jwt() decrypts the stored JWT."""
        now = datetime.now(timezone.utc)
        session = Session(
            id=TEST_SESSION_ID,
            user_id="user_123",
            library_id="lib_456",
            stored_jwt=TEST_ENCRYPTED_JWT,
            device_type="iOS",
            device_os="iOS 17.4",
            app_version="1.94.0",
            created_at=now,
            updated_at=now,
            is_pending_sync_reset=False,
        )

        with patch("services.session_store.decrypt_jwt") as mock_decrypt:
            mock_decrypt.return_value = "decrypted.jwt.token"
            result = session.get_jwt()

            mock_decrypt.assert_called_once_with(TEST_ENCRYPTED_JWT)
            assert result == "decrypted.jwt.token"


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
        with patch("services.session_store.encrypt_jwt") as mock_encrypt:
            mock_encrypt.return_value = TEST_ENCRYPTED_JWT

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
            assert session.stored_jwt == TEST_ENCRYPTED_JWT
            assert session.device_type == "iOS"
            assert session.device_os == "iOS 17.4"
            assert session.app_version == "1.94.0"
            assert session.is_pending_sync_reset is False
            # Session ID should be a UUID
            assert isinstance(session.id, UUID)

            # Verify encrypt_jwt was called
            mock_encrypt.assert_called_once_with("test.jwt")

            # Verify pipeline was executed
            mock_redis.pipeline.return_value.execute.assert_called_once()

    @pytest.mark.anyio
    async def test_create_session_with_expiry(self, session_store, mock_redis):
        """Test creating a session with TTL sets TTL on both session and checkpoint keys."""
        with patch("services.session_store.encrypt_jwt") as mock_encrypt:
            mock_encrypt.return_value = TEST_ENCRYPTED_JWT

            # Set expiry to 1 hour in the future
            expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

            session = await session_store.create(
                jwt_token="test.jwt",
                user_id="user_123",
                library_id="lib_456",
                device_type="iOS",
                device_os="iOS 17.4",
                app_version="1.94.0",
                expires_at=expires_at,
            )

            # Verify expire was called on pipeline for both session and checkpoint keys
            mock_pipeline = mock_redis.pipeline.return_value
            assert mock_pipeline.expire.call_count == 2
            expire_calls = [call[0][0] for call in mock_pipeline.expire.call_args_list]
            session_key = str(session.id)
            assert f"session:{session_key}" in expire_calls
            assert f"session:{session_key}:checkpoints" in expire_calls

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
        with patch("services.session_store.encrypt_jwt") as mock_encrypt:
            mock_encrypt.return_value = TEST_ENCRYPTED_JWT

            session = await session_store.create(
                jwt_token="test.jwt",
                user_id="user_123",
                library_id="lib_456",
                device_type="iOS",
                device_os="iOS 17.4",
                app_version="1.94.0",
            )

            mock_pipeline = mock_redis.pipeline.return_value
            session_key = str(session.id)

            # Verify hset was called with session data
            mock_pipeline.hset.assert_called()
            hset_call = mock_pipeline.hset.call_args
            assert f"session:{session_key}" == hset_call[0][0]

            # Verify sadd was called to add to user's session set
            mock_pipeline.sadd.assert_called_with(
                f"user:{session.user_id}:sessions", session_key
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
            "user_id": "user_123",
            "library_id": "lib_456",
            "stored_jwt": TEST_ENCRYPTED_JWT,
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        session = await session_store.get_by_id(str(TEST_SESSION_ID))

        assert session is not None
        assert session.id == TEST_SESSION_ID
        assert session.user_id == "user_123"
        assert session.library_id == "lib_456"
        assert session.stored_jwt == TEST_ENCRYPTED_JWT

    @pytest.mark.anyio
    async def test_get_session_not_found(self, session_store, mock_redis):
        """Test getting a non-existent session."""
        mock_redis.hgetall.return_value = {}

        session = await session_store.get_by_id(str(TEST_SESSION_ID))

        assert session is None

    @pytest.mark.anyio
    async def test_get_by_id(self, session_store, mock_redis):
        """Test getting session by ID."""
        mock_redis.hgetall.return_value = {
            "user_id": "user_123",
            "library_id": "lib_456",
            "stored_jwt": TEST_ENCRYPTED_JWT,
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        session = await session_store.get_by_id(str(TEST_SESSION_ID))

        assert session is not None
        assert session.id == TEST_SESSION_ID

    @pytest.mark.anyio
    async def test_get_by_id_invalid_uuid_returns_none(self, session_store, mock_redis):
        """Test getting session with invalid UUID returns None."""
        mock_redis.hgetall.return_value = {
            "user_id": "user_123",
            "library_id": "lib_456",
            "stored_jwt": TEST_ENCRYPTED_JWT,
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        # Invalid UUID string
        session = await session_store.get_by_id("not-a-valid-uuid")

        assert session is None

    @pytest.mark.anyio
    async def test_get_by_id_redis_error_raises_session_store_error(
        self, session_store, mock_redis
    ):
        """Test that Redis errors are wrapped in SessionStoreError."""
        mock_redis.hgetall.side_effect = redis.exceptions.ConnectionError(
            "Connection refused"
        )

        with pytest.raises(SessionStoreError) as exc_info:
            await session_store.get_by_id(str(TEST_SESSION_ID))

        assert "Failed to retrieve session" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, redis.exceptions.ConnectionError)


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
        mock_redis.smembers.return_value = {
            str(TEST_SESSION_ID),
            str(TEST_SESSION_ID_2),
        }

        # Pipeline returns list of hgetall results
        mock_pipeline = mock_redis.pipeline.return_value
        mock_pipeline.execute.return_value = [
            {
                "user_id": "user_123",
                "library_id": "lib_456",
                "stored_jwt": TEST_ENCRYPTED_JWT,
                "device_type": "iOS",
                "device_os": "iOS 17.4",
                "app_version": "1.94.0",
                "created_at": "2025-01-20T10:00:00+00:00",
                "updated_at": "2025-01-20T10:30:00+00:00",
                "is_pending_sync_reset": "0",
            },
            {
                "user_id": "user_123",
                "library_id": "lib_456",
                "stored_jwt": TEST_ENCRYPTED_JWT,
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
        # Use a valid UUID for the orphaned session token (will still be orphaned due to empty data)
        orphaned_uuid = "660e8400-e29b-41d4-a716-446655440001"
        mock_redis.smembers.return_value = {str(TEST_SESSION_ID), orphaned_uuid}

        # First pipeline for fetching - one valid session, one expired (empty dict)
        fetch_pipeline = MagicMock()
        fetch_pipeline.hgetall = MagicMock(
            return_value=fetch_pipeline
        )  # Chain returns self
        fetch_pipeline.execute = AsyncMock(
            return_value=[
                {
                    "user_id": "user_123",
                    "library_id": "lib_456",
                    "stored_jwt": TEST_ENCRYPTED_JWT,
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
        cleanup_pipeline.srem = MagicMock(return_value=cleanup_pipeline)
        cleanup_pipeline.zrem = MagicMock(return_value=cleanup_pipeline)
        cleanup_pipeline.execute = AsyncMock(return_value=[])

        mock_redis.pipeline = MagicMock(side_effect=[fetch_pipeline, cleanup_pipeline])

        sessions = await session_store.get_by_user("user_123")

        # Should only return the valid session
        assert len(sessions) == 1
        assert sessions[0].device_type == "iOS"

        # Cleanup pipeline should have been called
        cleanup_pipeline.srem.assert_called()
        cleanup_pipeline.zrem.assert_called()


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
            "user_id": "user_123",
            "library_id": "lib_456",
            "stored_jwt": TEST_ENCRYPTED_JWT,
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        result = await session_store.delete(str(TEST_SESSION_ID))

        assert result is True
        mock_redis.pipeline.return_value.execute.assert_called_once()

    @pytest.mark.anyio
    async def test_delete_session_not_found(self, session_store, mock_redis):
        """Test deleting a non-existent session."""
        mock_redis.hgetall.return_value = {}

        result = await session_store.delete(str(TEST_SESSION_ID))

        assert result is False

    @pytest.mark.anyio
    async def test_delete_verifies_pipeline_commands(self, session_store, mock_redis):
        """Test that delete issues correct pipeline commands including checkpoint cleanup."""
        session_token = str(TEST_SESSION_ID)
        mock_redis.hgetall.return_value = {
            "user_id": "user_123",
            "library_id": "lib_456",
            "stored_jwt": TEST_ENCRYPTED_JWT,
            "device_type": "iOS",
            "device_os": "iOS 17.4",
            "app_version": "1.94.0",
            "created_at": "2025-01-20T10:00:00+00:00",
            "updated_at": "2025-01-20T10:30:00+00:00",
            "is_pending_sync_reset": "0",
        }

        await session_store.delete(session_token)

        mock_pipeline = mock_redis.pipeline.return_value

        # Verify delete was called for session and checkpoints
        assert mock_pipeline.delete.call_count == 2
        delete_calls = [call[0][0] for call in mock_pipeline.delete.call_args_list]
        assert f"session:{session_token}" in delete_calls
        assert f"session:{session_token}:checkpoints" in delete_calls

        # Verify srem was called to remove from user's session set
        mock_pipeline.srem.assert_called_with("user:user_123:sessions", session_token)

        # Verify zrem was called for the activity index
        mock_pipeline.zrem.assert_called_with("sessions:by_updated_at", session_token)


def _split_eval_args(numkeys, keys_and_args):
    """Split an EVAL payload into (keys, token, score, fields)."""
    keys = list(keys_and_args[:numkeys])
    token, score, *pairs = keys_and_args[numkeys:]
    return keys, token, score, dict(zip(pairs[::2], pairs[1::2]))


def _decode_eval(mock_redis):
    """Decode the recorded EVAL into (keys, token, score, fields)."""
    script, numkeys, *rest = mock_redis.eval.call_args.args
    assert script is _UPDATE_IF_EXISTS_LUA
    return _split_eval_args(numkeys, rest)


class TestSessionStoreConditionalWrites:
    """Every session hash writer goes through the atomic conditional update."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock async Redis client whose EVAL reports a live session."""
        mock = AsyncMock()
        mock.eval.return_value = 1
        return mock

    @pytest.fixture
    def session_store(self, mock_redis):
        """Create SessionStore with mocked Redis."""
        return SessionStore(mock_redis)

    @pytest.mark.anyio
    async def test_update_activity_stamps_updated_at_and_index(
        self, session_store, mock_redis
    ):
        """Test update_activity writes updated_at and the activity index score."""
        session_token = str(TEST_SESSION_ID)

        result = await session_store.update_activity(session_token)

        assert result is True
        keys, token, score, fields = _decode_eval(mock_redis)
        assert keys == [f"session:{session_token}", "sessions:by_updated_at"]
        assert token == session_token
        assert set(fields) == {"updated_at"}
        assert datetime.fromisoformat(
            fields["updated_at"]
        ).timestamp() == pytest.approx(float(score))

    @pytest.mark.anyio
    async def test_update_stored_jwt_writes_encrypted_jwt_and_activity(
        self, session_store, mock_redis
    ):
        """Test update_stored_jwt stores the encrypted JWT and touches activity."""
        session_token = str(TEST_SESSION_ID)

        with patch("services.session_store.encrypt_jwt") as mock_encrypt:
            mock_encrypt.return_value = "new_encrypted_jwt"

            result = await session_store.update_stored_jwt(
                session_token, "new.jwt.token"
            )

        assert result is True
        mock_encrypt.assert_called_once_with("new.jwt.token")
        _keys, _token, score, fields = _decode_eval(mock_redis)
        assert fields["stored_jwt"] == "new_encrypted_jwt"
        assert datetime.fromisoformat(
            fields["updated_at"]
        ).timestamp() == pytest.approx(float(score))

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "write,expected_fields",
        [
            pytest.param(
                lambda store, token: store.update_library_id(
                    token, "lib_old", "lib_new", from_choice=True
                ),
                {
                    "library_id": "lib_new",
                    "library_checked_at": "1700000000.0",
                    "library_from_choice": "1",
                },
                id="update_library_id",
            ),
            pytest.param(
                lambda store, token: store.switch_library(
                    token, "lib_old", "lib_new", from_choice=False
                ),
                {
                    "library_id": "lib_new",
                    "library_checked_at": "1700000000.0",
                    "library_from_choice": "0",
                    "is_pending_sync_reset": "1",
                },
                id="switch_library",
            ),
        ],
    )
    async def test_library_writes_hold_only_while_the_previous_library_is_cached(
        self, session_store, mock_redis, write, expected_fields
    ):
        """Library writes record when and how the library was resolved, and
        apply only while the session still holds the library they resolved
        from, so a stale request cannot undo a concurrent switch. A switch also
        flags a sync reset: checkpoints belong to the previous library."""
        session_token = str(TEST_SESSION_ID)

        with patch("services.session_store.time.time", return_value=1700000000.0):
            result = await write(session_store, session_token)

        assert result is True
        script, numkeys, key, expected, *pairs = mock_redis.eval.call_args.args
        assert script is _SET_LIBRARY_IF_LUA
        assert (numkeys, key, expected) == (1, f"session:{session_token}", "lib_old")
        assert dict(zip(pairs[::2], pairs[1::2])) == expected_fields

    @pytest.mark.anyio
    async def test_switch_library_reports_a_session_that_already_moved(
        self, session_store, mock_redis
    ):
        mock_redis.eval.return_value = 0

        result = await session_store.switch_library(
            str(TEST_SESSION_ID), "lib_old", "lib_new", from_choice=False
        )

        assert result is False

    @pytest.mark.anyio
    async def test_forget_library_clears_and_flags_sync_reset(
        self, session_store, mock_redis
    ):
        """The vanished library's local copy and checkpoints must not be
        resumed against its replacement, so forgetting also queues a reset."""
        result = await session_store.forget_library(str(TEST_SESSION_ID))

        assert result is True
        _keys, _token, _score, fields = _decode_eval(mock_redis)
        assert fields == {"library_id": "", "is_pending_sync_reset": "1"}

    @pytest.mark.anyio
    @pytest.mark.parametrize("pending,expected", [(True, "1"), (False, "0")])
    async def test_set_pending_sync_reset(
        self, session_store, mock_redis, pending, expected
    ):
        """Test set_pending_sync_reset writes the flag in both directions."""
        result = await session_store.set_pending_sync_reset(
            str(TEST_SESSION_ID), pending
        )

        assert result is True
        _keys, _token, _score, fields = _decode_eval(mock_redis)
        assert fields == {"is_pending_sync_reset": expected}

    @pytest.mark.anyio
    @pytest.mark.parametrize("writer", WRITERS)
    async def test_writer_reports_missing_session(
        self, session_store, mock_redis, writer
    ):
        """Test each writer returns False when the session no longer exists."""
        mock_redis.eval.return_value = 0

        with patch("services.session_store.encrypt_jwt", return_value="enc"):
            result = await writer(session_store, str(TEST_SESSION_ID))

        assert result is False


def _full_session_data() -> dict[str, str]:
    """Build a complete session hash as SessionStore.create would write it."""
    now = datetime.now(timezone.utc)
    return Session(
        id=TEST_SESSION_ID,
        user_id="user_123",
        library_id="lib_456",
        stored_jwt=TEST_ENCRYPTED_JWT,
        device_type="iOS",
        device_os="iOS 17.4",
        app_version="1.94.0",
        created_at=now,
        updated_at=now,
        is_pending_sync_reset=False,
    ).to_dict()


class _RacingRedis:
    """In-memory Redis stand-in that races a session delete against one writer."""

    def __init__(
        self, session_token: str, data: dict[str, str], *, deleted_first: bool = False
    ):
        self.key = f"session:{session_token}"
        self.hashes: dict[str, dict[str, str]] = (
            {} if deleted_first else {self.key: dict(data)}
        )
        self.zsets: dict[str, dict[str, float]] = {}
        self._delete_pending = not deleted_first

    def _after_round_trip(self) -> None:
        """Deliver the concurrent delete once, after the first command is served."""
        if self._delete_pending:
            self._delete_pending = False
            self.hashes.pop(self.key, None)

    def _hset(self, name, mapping) -> None:
        self.hashes.setdefault(name, {}).update(mapping)

    def _zadd(self, name, mapping) -> None:
        self.zsets.setdefault(name, {}).update(mapping)

    async def eval(self, script, numkeys, *keys_and_args) -> int:
        # Transcribes _UPDATE_IF_EXISTS_LUA; the script itself is never run here.
        assert script is _UPDATE_IF_EXISTS_LUA
        keys, token, score, fields = _split_eval_args(numkeys, keys_and_args)
        result = 0
        if keys[0] in self.hashes:
            self._hset(keys[0], fields)
            if score:
                self._zadd(keys[1], {token: float(score)})
            result = 1
        self._after_round_trip()
        return result


class TestSessionStoreConcurrentDelete:
    """A delete racing a writer must never leave a partial session hash."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("writer", WRITERS)
    async def test_delete_after_first_round_trip_leaves_no_partial_hash(self, writer):
        """Test a delete landing mid-write leaves no resurrected partial hash."""
        session_token = str(TEST_SESSION_ID)
        racing_redis = _RacingRedis(session_token, _full_session_data())
        session_store = SessionStore(racing_redis)

        with patch("services.session_store.encrypt_jwt", return_value="enc"):
            await writer(session_store, session_token)

        stored = racing_redis.hashes.get(f"session:{session_token}")
        assert stored is None or _REQUIRED_SESSION_FIELDS <= set(stored)

    @pytest.mark.anyio
    @pytest.mark.parametrize("writer", WRITERS)
    async def test_delete_before_write_touches_neither_hash_nor_index(self, writer):
        """Test a writer finding no session writes neither the hash nor the index."""
        session_token = str(TEST_SESSION_ID)
        racing_redis = _RacingRedis(
            session_token, _full_session_data(), deleted_first=True
        )
        session_store = SessionStore(racing_redis)

        with patch("services.session_store.encrypt_jwt", return_value="enc"):
            result = await writer(session_store, session_token)

        assert result is False
        assert racing_redis.hashes == {}
        assert racing_redis.zsets == {}


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
        """Test getting stale session tokens."""
        mock_redis.zrangebyscore.return_value = [
            str(TEST_SESSION_ID),
            str(TEST_SESSION_ID_2),
        ]

        stale_tokens = await session_store.get_stale_sessions(days=90)

        assert len(stale_tokens) == 2
        assert str(TEST_SESSION_ID) in stale_tokens
        assert str(TEST_SESSION_ID_2) in stale_tokens

    @pytest.mark.anyio
    async def test_cleanup_stale_sessions(self, session_store, mock_redis):
        """Test cleaning up stale sessions and checkpoints using pipeline."""
        mock_redis.zrangebyscore.return_value = [str(TEST_SESSION_ID)]

        # First pipeline for fetching session data
        fetch_pipeline = MagicMock()
        fetch_pipeline.execute = AsyncMock(
            return_value=[
                {
                    "user_id": "user_123",
                    "library_id": "lib_456",
                    "stored_jwt": TEST_ENCRYPTED_JWT,
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
        # Verify both session and checkpoints are deleted
        assert delete_pipeline.delete.call_count == 2
        delete_calls = [call[0][0] for call in delete_pipeline.delete.call_args_list]
        assert f"session:{TEST_SESSION_ID}" in delete_calls
        assert f"session:{TEST_SESSION_ID}:checkpoints" in delete_calls
        delete_pipeline.srem.assert_called()
        delete_pipeline.zrem.assert_called()

    @pytest.mark.anyio
    async def test_cleanup_stale_sessions_handles_already_expired(
        self, session_store, mock_redis
    ):
        """Test cleanup handles sessions that expired via TTL."""
        mock_redis.zrangebyscore.return_value = [
            str(TEST_SESSION_ID),
            "already_expired",
        ]

        # First pipeline - one valid, one already expired (empty)
        fetch_pipeline = MagicMock()
        fetch_pipeline.execute = AsyncMock(
            return_value=[
                {
                    "user_id": "user_123",
                    "library_id": "lib_456",
                    "stored_jwt": TEST_ENCRYPTED_JWT,
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
