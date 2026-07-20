"""Tests for the trash router (restore-by-ids, restore-all, empty-trash).

The router calls the Gumnut API trash primitives directly through the
``AsyncGumnut`` client (``client.post``, ``client.delete``,
``client.assets.list(state="trashed")``). Tests mock those entry points and
assert on the bulk call shapes, WebSocket event shapes, and returned counts.
"""

from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from socketio.exceptions import SocketIOError

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS, GUMNUT_API_MAX_PAGE_SIZE
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

        asset_ids = [uuid4() for _ in range(GUMNUT_API_MAX_BULK_IDS + 50)]
        request = BulkIdsDto(ids=asset_ids)

        with patch(
            "routers.api.trash.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            result = await restore_assets(
                request, client=mock_client, current_user_id=uuid4()
            )

        assert result.count == len(asset_ids)
        assert mock_client.post.await_count == 2
        chunk_sizes = [
            len(call.kwargs["body"]["ids"]) for call in mock_client.post.await_args_list
        ]
        assert chunk_sizes == [GUMNUT_API_MAX_BULK_IDS, 50]
        # One batched on_asset_restore event per chunk.
        assert mock_emit.await_count == 2

    @pytest.mark.anyio
    async def test_websocket_error_does_not_fail_restore(self):
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        request = BulkIdsDto(ids=[uuid4()])

        # Patch the underlying emit so the SocketIOError originates *inside*
        # emit_user_event (which now swallows it centrally).
        with patch(
            "services.websockets._emit_event",
            new_callable=AsyncMock,
            side_effect=SocketIOError("ws error"),
        ):
            result = await restore_assets(
                request, client=mock_client, current_user_id=uuid4()
            )

        assert result.count == 1

    @pytest.mark.anyio
    async def test_propagates_sdk_error(self):
        """SDK errors on bulk restore bubble to the global GumnutError handler.

        Pins the no-swallow contract — a future refactor that wraps the bulk
        call in try/except must break this test.
        """
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.post = AsyncMock(side_effect=make_sdk_status_error(500, "boom"))

        request = BulkIdsDto(ids=[uuid4()])

        with patch("routers.api.trash.emit_user_event", new_callable=AsyncMock):
            with pytest.raises(APIStatusError):
                await restore_assets(
                    request, client=mock_client, current_user_id=uuid4()
                )


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
        mock_client.assets.list.assert_called_once_with(
            state="trashed", limit=GUMNUT_API_MAX_PAGE_SIZE
        )

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
    async def test_websocket_error_does_not_fail_restore_trash(self):
        """SocketIOError from emit must not fail the restore-all flow."""
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        gumnut_ids = [uuid_to_gumnut_asset_id(uuid4()) for _ in range(2)]
        trashed_assets = [_make_trashed_asset_mock(gid) for gid in gumnut_ids]
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage(trashed_assets))

        with patch(
            "services.websockets._emit_event",
            new_callable=AsyncMock,
            side_effect=SocketIOError("ws error"),
        ):
            result = await restore_trash(client=mock_client, current_user_id=uuid4())

        assert result.count == 2

    @pytest.mark.anyio
    async def test_chunks_large_trash_lists(self):
        mock_client = Mock()
        mock_client.post = AsyncMock(return_value=None)

        total = GUMNUT_API_MAX_BULK_IDS * 2 + 50
        gumnut_ids = [uuid_to_gumnut_asset_id(uuid4()) for _ in range(total)]
        trashed_assets = [_make_trashed_asset_mock(gid) for gid in gumnut_ids]
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage(trashed_assets))

        with patch(
            "routers.api.trash.emit_user_event", new_callable=AsyncMock
        ) as mock_emit:
            result = await restore_trash(client=mock_client, current_user_id=uuid4())

        assert result.count == total
        assert mock_client.post.await_count == 3
        chunk_sizes = [
            len(call.kwargs["body"]["ids"]) for call in mock_client.post.await_args_list
        ]
        assert chunk_sizes == [
            GUMNUT_API_MAX_BULK_IDS,
            GUMNUT_API_MAX_BULK_IDS,
            50,
        ]
        assert mock_emit.await_count == 3

    @pytest.mark.anyio
    async def test_propagates_sdk_error(self):
        """SDK errors on bulk restore bubble to the global GumnutError handler."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.post = AsyncMock(side_effect=make_sdk_status_error(500, "boom"))

        gumnut_ids = [uuid_to_gumnut_asset_id(uuid4())]
        trashed_assets = [_make_trashed_asset_mock(gid) for gid in gumnut_ids]
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage(trashed_assets))

        with patch("routers.api.trash.emit_user_event", new_callable=AsyncMock):
            with pytest.raises(APIStatusError):
                await restore_trash(client=mock_client, current_user_id=uuid4())


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
        # `empty_trash` calls `emit_user_event_per_id`, which in turn fans out
        # `emit_user_event` once per id. Patch at the websockets module so the
        # per-id call count is observable.
        with patch(
            "services.websockets.emit_user_event", new_callable=AsyncMock
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

        total = GUMNUT_API_MAX_BULK_IDS * 2 + 50
        gumnut_ids = [uuid_to_gumnut_asset_id(uuid4()) for _ in range(total)]
        trashed_assets = [_make_trashed_asset_mock(gid) for gid in gumnut_ids]
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage(trashed_assets))

        with patch("routers.api.trash.emit_user_event", new_callable=AsyncMock):
            result = await empty_trash(client=mock_client, current_user_id=uuid4())

        assert result.count == total
        assert mock_client.delete.await_count == 3
        chunk_sizes = [
            len(call.kwargs["body"]["ids"])
            for call in mock_client.delete.await_args_list
        ]
        assert chunk_sizes == [
            GUMNUT_API_MAX_BULK_IDS,
            GUMNUT_API_MAX_BULK_IDS,
            50,
        ]

    @pytest.mark.anyio
    async def test_websocket_error_does_not_fail_empty_trash(self):
        """SocketIOError from emit must not fail the empty-trash flow."""
        mock_client = Mock()
        mock_client.delete = AsyncMock(return_value=None)

        gumnut_ids = [uuid_to_gumnut_asset_id(uuid4()) for _ in range(2)]
        trashed_assets = [_make_trashed_asset_mock(gid) for gid in gumnut_ids]
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage(trashed_assets))

        with patch(
            "services.websockets._emit_event",
            new_callable=AsyncMock,
            side_effect=SocketIOError("ws error"),
        ):
            result = await empty_trash(client=mock_client, current_user_id=uuid4())

        assert result.count == 2

    @pytest.mark.anyio
    async def test_propagates_sdk_error(self):
        """SDK errors on bulk hard-delete bubble to the global GumnutError handler."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.delete = AsyncMock(side_effect=make_sdk_status_error(500, "boom"))

        gumnut_ids = [uuid_to_gumnut_asset_id(uuid4())]
        trashed_assets = [_make_trashed_asset_mock(gid) for gid in gumnut_ids]
        mock_client.assets.list = Mock(return_value=MockSyncCursorPage(trashed_assets))

        with patch("routers.api.trash.emit_user_event", new_callable=AsyncMock):
            with pytest.raises(APIStatusError):
                await empty_trash(client=mock_client, current_user_id=uuid4())
