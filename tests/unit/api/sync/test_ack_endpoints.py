"""Tests for sync ack CRUD endpoints."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from routers.api.sync.routes import delete_sync_ack, get_sync_ack, send_sync_ack
from routers.immich_models import SyncAckDeleteDto, SyncAckSetDto, SyncEntityType
from services.checkpoint_store import Checkpoint, CheckpointStore
from services.session_store import SessionStore
from tests.unit.api.sync.conftest import TEST_SESSION_UUID


class TestGetSyncAck:
    """Tests for the get_sync_ack endpoint."""

    @pytest.mark.anyio
    async def test_returns_checkpoints_as_ack_dtos(self):
        """Stored checkpoints are returned as SyncAckDto list."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        checkpoint_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AssetV1,
            updated_at=checkpoint_time,
            cursor="event_cursor_abc",
        )
        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_checkpoint_store.get_all.return_value = [checkpoint]

        result = await get_sync_ack(
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        assert len(result) == 1
        assert result[0].type == SyncEntityType.AssetV1
        # Ack format: "SyncEntityType|cursor|"
        assert result[0].ack == "AssetV1|event_cursor_abc|"
        mock_checkpoint_store.get_all.assert_called_once_with(TEST_SESSION_UUID)

    @pytest.mark.anyio
    async def test_returns_checkpoints_without_cursor(self):
        """Checkpoints without cursor return empty string in ack."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        checkpoint_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AssetV1,
            updated_at=checkpoint_time,
            cursor=None,
        )
        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_checkpoint_store.get_all.return_value = [checkpoint]

        result = await get_sync_ack(
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        assert len(result) == 1
        # Ack format with empty cursor: "SyncEntityType||"
        assert result[0].ack == "AssetV1||"

    @pytest.mark.anyio
    async def test_returns_empty_list_when_no_checkpoints(self):
        """Returns empty list when no checkpoints exist for session."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_checkpoint_store.get_all.return_value = []

        result = await get_sync_ack(
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        assert result == []


class TestSendSyncAck:
    """Tests for the send_sync_ack endpoint."""

    @pytest.mark.anyio
    async def test_stores_valid_checkpoints(self):
        """Valid acks are parsed and stored as checkpoints."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_session_store = AsyncMock(spec=SessionStore)

        # v2 ack format: "SyncEntityType|cursor|"
        request = SyncAckSetDto(acks=["AssetV1|event_cursor_abc|"])

        await send_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # Verify checkpoint was stored
        mock_checkpoint_store.set_many.assert_called_once()
        call_args = mock_checkpoint_store.set_many.call_args
        assert call_args[0][0] == TEST_SESSION_UUID
        checkpoints = call_args[0][1]
        assert len(checkpoints) == 1
        # Expecting 2-tuple: (entity_type, cursor)
        assert checkpoints[0] == (SyncEntityType.AssetV1, "event_cursor_abc")

        # Verify session activity was updated
        mock_session_store.update_activity.assert_called_once_with(
            str(TEST_SESSION_UUID)
        )

    @pytest.mark.anyio
    async def test_stores_checkpoint_with_empty_cursor(self):
        """Acks without cursor store empty string."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_session_store = AsyncMock(spec=SessionStore)

        # Empty cursor
        request = SyncAckSetDto(acks=["AssetV1||"])

        await send_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        call_args = mock_checkpoint_store.set_many.call_args
        checkpoints = call_args[0][1]
        assert len(checkpoints) == 1
        # Empty cursor should be stored as empty string
        assert checkpoints[0] == (SyncEntityType.AssetV1, "")

    @pytest.mark.anyio
    async def test_handles_sync_reset_ack(self):
        """SyncResetV1 ack clears pending reset flag and deletes all checkpoints."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_session_store = AsyncMock(spec=SessionStore)

        # SyncResetV1 ack with cursor
        request = SyncAckSetDto(acks=["SyncResetV1|reset|"])

        await send_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # Verify sync reset was handled
        mock_session_store.set_pending_sync_reset.assert_called_once_with(
            str(TEST_SESSION_UUID), False
        )
        mock_checkpoint_store.delete_all.assert_called_once_with(TEST_SESSION_UUID)
        mock_session_store.update_activity.assert_called_once_with(
            str(TEST_SESSION_UUID)
        )

        # Verify set_many was NOT called (early return after reset)
        mock_checkpoint_store.set_many.assert_not_called()

    @pytest.mark.anyio
    async def test_skips_malformed_acks(self):
        """Malformed acks are skipped, valid acks are still processed."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_session_store = AsyncMock(spec=SessionStore)

        request = SyncAckSetDto(
            acks=[
                "malformed",  # Too few parts - skipped
                "AssetV1|event_cursor_abc|",  # Valid
            ]
        )

        await send_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # Only the valid checkpoint should be stored
        call_args = mock_checkpoint_store.set_many.call_args
        checkpoints = call_args[0][1]
        assert len(checkpoints) == 1
        assert checkpoints[0] == (SyncEntityType.AssetV1, "event_cursor_abc")

    @pytest.mark.anyio
    async def test_does_not_store_when_all_acks_malformed(self):
        """When all acks are malformed, set_many is not called."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_session_store = AsyncMock(spec=SessionStore)

        request = SyncAckSetDto(acks=["malformed"])

        await send_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # set_many should not be called since no valid checkpoints
        mock_checkpoint_store.set_many.assert_not_called()


class TestDeleteSyncAck:
    """Tests for the delete_sync_ack endpoint."""

    @pytest.mark.anyio
    async def test_deletes_specific_checkpoint_types(self):
        """Deletes only the specified checkpoint types."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)

        request = SyncAckDeleteDto(
            types=[SyncEntityType.AssetV1, SyncEntityType.AlbumV1]
        )

        await delete_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        mock_checkpoint_store.delete.assert_called_once_with(
            TEST_SESSION_UUID, [SyncEntityType.AssetV1, SyncEntityType.AlbumV1]
        )
        mock_checkpoint_store.delete_all.assert_not_called()

    @pytest.mark.anyio
    async def test_does_nothing_when_types_empty(self):
        """Does nothing when types list is empty (matches Immich behavior)."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)

        request = SyncAckDeleteDto(types=[])

        await delete_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        # Empty list = no-op, matching Immich's behavior
        mock_checkpoint_store.delete_all.assert_not_called()
        mock_checkpoint_store.delete.assert_not_called()

    @pytest.mark.anyio
    async def test_deletes_all_checkpoints_when_types_none(self):
        """Deletes all checkpoints when types is None."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)

        request = SyncAckDeleteDto(types=None)

        await delete_sync_ack(
            request=request,
            http_request=mock_request,
            checkpoint_store=mock_checkpoint_store,
        )

        mock_checkpoint_store.delete_all.assert_called_once_with(TEST_SESSION_UUID)
        mock_checkpoint_store.delete.assert_not_called()
