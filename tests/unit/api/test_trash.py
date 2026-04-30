"""Tests for the trash router (restore-by-ids, restore-all, empty-trash).

The router calls the photos-api trash primitives directly through the
``AsyncGumnut`` client (``client.post``, ``client.delete``,
``client.assets.list(state="trashed")``). Tests mock those entry points and
assert on the bulk call shapes, WebSocket event shapes, and returned counts.
"""

from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from socketio.exceptions import SocketIOError

from routers.api.trash import empty_trash, restore_assets, restore_trash
from routers.immich_models import BulkIdsDto
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    uuid_to_gumnut_asset_id,
)
from services.websockets import WebSocketEvent
from tests.conftest import MockSyncCursorPage


def _make_trashed_asset_mock(gumnut_id: str) -> Mock:
    """Minimal mock of the SDK's AssetResponse for state='trashed' enumeration."""
    asset = Mock()
    asset.id = gumnut_id
    return asset


class TestRestoreAssets:
    """POST /api/trash/restore/assets — restore by explicit ids."""

    @pytest.mark.anyio
    async def test_empty_id_list_returns_zero_without_calling_backend(self):
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        with patch("routers.api.trash.emit_user_event", new_callable=AsyncMock):
            result = await restore_assets(
                BulkIdsDto(ids=[]), client=mock_client, current_user_id=uuid4()
            )

        assert result.count == 0
        mock_client.post.assert_not_awaited()

    @pytest.mark.anyio
    async def test_calls_backend_restore_with_gumnut_ids(self):
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        asset_ids = [uuid4(), uuid4()]
        request = BulkIdsDto(ids=asset_ids)

        with patch("routers.api.trash.emit_user_event", new_callable=AsyncMock):
            result = await restore_assets(
                request, client=mock_client, current_user_id=uuid4()
            )

        assert result.count == 2
        mock_client.post.assert_awaited_once()
        call = mock_client.post.await_args
        assert call.args[0] == "/api/assets/restore"
        body = call.kwargs["body"]
        assert set(body["ids"]) == {uuid_to_gumnut_asset_id(uid) for uid in asset_ids}

    @pytest.mark.anyio
    async def test_emits_single_batched_restore_event_per_chunk(self):
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        asset_ids = [uuid4(), uuid4(), uuid4()]
        request = BulkIdsDto(ids=asset_ids)
        current_user_id = uuid4()

        with patch(
            "routers.api.trash.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            await restore_assets(
                request, client=mock_client, current_user_id=current_user_id
            )

        assert mock_emit.await_count == 1
        event, user_id, payload = mock_emit.await_args_list[0].args
        assert event == WebSocketEvent.ASSET_RESTORE
        assert user_id == str(current_user_id)
        assert payload == [str(uid) for uid in asset_ids]

    @pytest.mark.anyio
    async def test_chunks_when_over_cap(self):
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        # 150 ids → 100 + 50.
        asset_ids = [uuid4() for _ in range(150)]
        request = BulkIdsDto(ids=asset_ids)

        with patch(
            "routers.api.trash.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            result = await restore_assets(
                request, client=mock_client, current_user_id=uuid4()
            )

        assert result.count == 150
        assert mock_client.post.await_count == 2
        chunk_sizes = [
            len(call.kwargs["body"]["ids"]) for call in mock_client.post.await_args_list
        ]
        assert chunk_sizes == [100, 50]
        # One batched on_asset_restore event per chunk.
        assert mock_emit.await_count == 2

    @pytest.mark.anyio
    async def test_websocket_error_does_not_fail_restore(self):
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        request = BulkIdsDto(ids=[uuid4()])

        with patch(
            "routers.api.trash.emit_user_event",
            new_callable=AsyncMock,
            side_effect=SocketIOError("ws error"),
        ):
            result = await restore_assets(
                request, client=mock_client, current_user_id=uuid4()
            )

        assert result.count == 1


class TestRestoreTrash:
    """POST /api/trash/restore — restore every trashed asset for the caller."""

    @pytest.mark.anyio
    async def test_no_trashed_assets_returns_zero(self):
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage([]))

        with patch("routers.api.trash.emit_user_event", new_callable=AsyncMock):
            result = await restore_trash(client=mock_client, current_user_id=uuid4())

        assert result.count == 0
        mock_client.post.assert_not_awaited()
        mock_client.assets.list.assert_called_once_with(state="trashed", limit=100)

    @pytest.mark.anyio
    async def test_restores_enumerated_ids_and_emits_per_chunk_event(self):
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        gumnut_ids = [uuid_to_gumnut_asset_id(uuid4()) for _ in range(3)]
        trashed_assets = [_make_trashed_asset_mock(gid) for gid in gumnut_ids]
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage(trashed_assets))

        current_user_id = uuid4()
        with patch(
            "routers.api.trash.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            result = await restore_trash(
                client=mock_client, current_user_id=current_user_id
            )

        assert result.count == 3
        mock_client.post.assert_awaited_once()
        call = mock_client.post.await_args
        assert call.args[0] == "/api/assets/restore"
        assert set(call.kwargs["body"]["ids"]) == set(gumnut_ids)

        # One batched on_asset_restore event with the chunk's UUID strings.
        assert mock_emit.await_count == 1
        event, user_id, payload = mock_emit.await_args_list[0].args
        assert event == WebSocketEvent.ASSET_RESTORE
        assert user_id == str(current_user_id)
        assert set(payload) == {str(safe_uuid_from_asset_id(gid)) for gid in gumnut_ids}

    @pytest.mark.anyio
    async def test_chunks_large_trash_lists(self):
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        # 250 trashed assets → 100 + 100 + 50 across three restore calls.
        gumnut_ids = [uuid_to_gumnut_asset_id(uuid4()) for _ in range(250)]
        trashed_assets = [_make_trashed_asset_mock(gid) for gid in gumnut_ids]
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage(trashed_assets))

        with patch(
            "routers.api.trash.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            result = await restore_trash(client=mock_client, current_user_id=uuid4())

        assert result.count == 250
        assert mock_client.post.await_count == 3
        chunk_sizes = [
            len(call.kwargs["body"]["ids"]) for call in mock_client.post.await_args_list
        ]
        assert chunk_sizes == [100, 100, 50]
        assert mock_emit.await_count == 3


class TestEmptyTrash:
    """POST /api/trash/empty — permanently delete every trashed asset."""

    @pytest.mark.anyio
    async def test_no_trashed_assets_returns_zero(self):
        mock_client = Mock()
        mock_client.delete = AsyncMock(return_value=None)
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage([]))

        with patch("routers.api.trash.emit_user_event", new_callable=AsyncMock):
            result = await empty_trash(client=mock_client, current_user_id=uuid4())

        assert result.count == 0
        mock_client.delete.assert_not_awaited()

    @pytest.mark.anyio
    async def test_purges_enumerated_ids_and_emits_per_id_delete_event(self):
        mock_client = Mock()
        mock_client.delete = AsyncMock(return_value=None)

        gumnut_ids = [uuid_to_gumnut_asset_id(uuid4()) for _ in range(3)]
        trashed_assets = [_make_trashed_asset_mock(gid) for gid in gumnut_ids]
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage(trashed_assets))

        current_user_id = uuid4()
        with patch(
            "routers.api.trash.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            result = await empty_trash(
                client=mock_client, current_user_id=current_user_id
            )

        assert result.count == 3
        mock_client.delete.assert_awaited_once()
        call = mock_client.delete.await_args
        assert call.args[0] == "/api/assets"
        assert set(call.kwargs["body"]["ids"]) == set(gumnut_ids)

        # One on_asset_delete per id (Immich's wire shape for permanent deletes).
        assert mock_emit.await_count == 3
        for emit_call in mock_emit.await_args_list:
            event, user_id, payload = emit_call.args
            assert event == WebSocketEvent.ASSET_DELETE
            assert user_id == str(current_user_id)
            # payload is a single asset UUID string
            assert payload in {str(safe_uuid_from_asset_id(gid)) for gid in gumnut_ids}

    @pytest.mark.anyio
    async def test_chunks_large_trash_lists(self):
        mock_client = Mock()
        mock_client.delete = AsyncMock(return_value=None)

        gumnut_ids = [uuid_to_gumnut_asset_id(uuid4()) for _ in range(150)]
        trashed_assets = [_make_trashed_asset_mock(gid) for gid in gumnut_ids]
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage(trashed_assets))

        with patch("routers.api.trash.emit_user_event", new_callable=AsyncMock):
            result = await empty_trash(client=mock_client, current_user_id=uuid4())

        assert result.count == 150
        assert mock_client.delete.await_count == 2
        chunk_sizes = [
            len(call.kwargs["body"]["ids"])
            for call in mock_client.delete.await_args_list
        ]
        assert chunk_sizes == [100, 50]
