"""Tests for sync ack CRUD endpoints."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from routers.api.sync.routes import delete_sync_ack, get_sync_ack, send_sync_ack
from routers.immich_models import SyncAckDeleteDto, SyncAckSetDto, SyncEntityType
from services.checkpoint_store import Checkpoint, CheckpointStore
from services.session_store import SessionStore
from tests.unit.api.sync.conftest import TEST_SESSION_UUID, create_mock_session

EPOCH = 2


def _request() -> Mock:
    request = Mock()
    request.state.session_token = str(TEST_SESSION_UUID)
    return request


def _session_store() -> AsyncMock:
    """A session store whose session is at sync epoch EPOCH."""
    store = AsyncMock(spec=SessionStore)
    session = create_mock_session()
    session.sync_epoch = session.client_epoch = EPOCH
    store.get_by_id.return_value = session
    return store


def _checkpoint_store() -> AsyncMock:
    store = AsyncMock(spec=CheckpointStore)
    store.set_many.return_value = True
    return store


async def _ack(acks: list[str]) -> tuple[AsyncMock, AsyncMock]:
    checkpoint_store = _checkpoint_store()
    session_store = _session_store()
    await send_sync_ack(
        request=SyncAckSetDto(acks=acks),
        http_request=_request(),
        checkpoint_store=checkpoint_store,
        session_store=session_store,
    )
    return checkpoint_store, session_store


class TestGetSyncAck:
    """Tests for the get_sync_ack endpoint."""

    @pytest.mark.anyio
    async def test_returns_checkpoints_of_the_session_epoch(self):
        """Stored checkpoints are returned as acks stamped with the epoch."""
        checkpoint_store = _checkpoint_store()
        checkpoint_store.get_all.return_value = [
            Checkpoint(
                entity_type=SyncEntityType.AssetV1,
                updated_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                cursor="event_cursor_abc",
            )
        ]

        result = await get_sync_ack(
            http_request=_request(),
            checkpoint_store=checkpoint_store,
            session_store=_session_store(),
        )

        assert len(result) == 1
        assert result[0].type == SyncEntityType.AssetV1
        assert result[0].ack == f"AssetV1|event_cursor_abc|{EPOCH}"
        checkpoint_store.get_all.assert_called_once_with(TEST_SESSION_UUID, EPOCH)

    @pytest.mark.anyio
    async def test_filters_checkpoints_without_cursor(self):
        """Checkpoints without cursor are excluded from response."""
        checkpoint_store = _checkpoint_store()
        checkpoint_store.get_all.return_value = [
            Checkpoint(
                entity_type=SyncEntityType.AssetV1,
                updated_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                cursor=None,
            )
        ]

        result = await get_sync_ack(
            http_request=_request(),
            checkpoint_store=checkpoint_store,
            session_store=_session_store(),
        )

        assert result == []


class TestSendSyncAck:
    """Tests for the send_sync_ack endpoint."""

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "ack",
        [
            pytest.param(f"AssetV1|event_cursor_abc|{EPOCH}", id="current-epoch"),
            # Issued before sync epochs: counts as the current one, so a client
            # mid-sync at the upgrade keeps its progress.
            pytest.param("AssetV1|event_cursor_abc|", id="no-epoch"),
        ],
    )
    async def test_stores_checkpoints_under_the_session_epoch(self, ack):
        checkpoint_store, session_store = await _ack([ack])

        checkpoint_store.set_many.assert_called_once_with(
            TEST_SESSION_UUID, EPOCH, [(SyncEntityType.AssetV1, "event_cursor_abc")]
        )
        session_store.update_activity.assert_called_once_with(str(TEST_SESSION_UUID))

    @pytest.mark.anyio
    async def test_drops_acks_for_an_epoch_the_session_left(self):
        """An ack from a stream of the library the session has left must not
        become a checkpoint of the library it syncs now."""
        checkpoint_store, session_store = await _ack(
            [f"AssetV1|stale_cursor|{EPOCH - 1}", f"AlbumV1|album_cursor|{EPOCH}"]
        )

        checkpoint_store.set_many.assert_called_once_with(
            TEST_SESSION_UUID, EPOCH, [(SyncEntityType.AlbumV1, "album_cursor")]
        )
        session_store.update_activity.assert_called_once()

    @pytest.mark.anyio
    async def test_skips_ack_with_empty_cursor(self):
        """Acks with empty cursor are skipped (not stored)."""
        checkpoint_store, _ = await _ack(["AssetV1||"])

        checkpoint_store.set_many.assert_not_called()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "ack,epoch",
        [
            pytest.param(f"SyncResetV1|reset|{EPOCH - 1}", EPOCH - 1, id="issued"),
            pytest.param("SyncResetV1|reset|", EPOCH, id="no-epoch"),
        ],
    )
    async def test_sync_reset_ack_records_the_epoch_it_was_issued_for(self, ack, epoch):
        checkpoint_store, session_store = await _ack([ack, f"AssetV1|ignored|{EPOCH}"])

        session_store.acknowledge_sync_reset.assert_called_once_with(
            str(TEST_SESSION_UUID), epoch
        )
        session_store.update_activity.assert_called_once_with(str(TEST_SESSION_UUID))
        checkpoint_store.set_many.assert_not_called()

    @pytest.mark.anyio
    async def test_skips_malformed_acks(self):
        """Malformed acks are skipped, valid acks are still processed."""
        checkpoint_store, _ = await _ack(["malformed", "AssetV1|event_cursor_abc|"])

        checkpoint_store.set_many.assert_called_once_with(
            TEST_SESSION_UUID, EPOCH, [(SyncEntityType.AssetV1, "event_cursor_abc")]
        )

    @pytest.mark.anyio
    async def test_does_not_store_when_all_acks_malformed(self):
        """When all acks are malformed, set_many is not called."""
        checkpoint_store, _ = await _ack(["malformed"])

        checkpoint_store.set_many.assert_not_called()


class TestDeleteSyncAck:
    """Tests for the delete_sync_ack endpoint."""

    async def _delete(self, types: list[SyncEntityType] | None) -> AsyncMock:
        checkpoint_store = _checkpoint_store()
        await delete_sync_ack(
            request=SyncAckDeleteDto(types=types),
            http_request=_request(),
            checkpoint_store=checkpoint_store,
            session_store=_session_store(),
        )
        return checkpoint_store

    @pytest.mark.anyio
    async def test_deletes_specific_checkpoint_types(self):
        """Deletes only the specified checkpoint types."""
        checkpoint_store = await self._delete(
            [SyncEntityType.AssetV1, SyncEntityType.AlbumV1]
        )

        checkpoint_store.delete.assert_called_once_with(
            TEST_SESSION_UUID, EPOCH, [SyncEntityType.AssetV1, SyncEntityType.AlbumV1]
        )
        checkpoint_store.delete_all.assert_not_called()

    @pytest.mark.anyio
    async def test_does_nothing_when_types_empty(self):
        """Does nothing when types list is empty (matches Immich behavior)."""
        checkpoint_store = await self._delete([])

        checkpoint_store.delete_all.assert_not_called()
        checkpoint_store.delete.assert_not_called()

    @pytest.mark.anyio
    async def test_deletes_all_checkpoints_when_types_none(self):
        """Deletes all checkpoints when types is None."""
        checkpoint_store = await self._delete(None)

        checkpoint_store.delete_all.assert_called_once_with(TEST_SESSION_UUID, EPOCH)
        checkpoint_store.delete.assert_not_called()
