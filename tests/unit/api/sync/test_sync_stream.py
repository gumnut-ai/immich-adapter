"""Tests for sync stream generation and endpoint."""

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock, call
from uuid import UUID

import pytest

from gumnut.types.face_response import FaceResponse

from routers.api.sync.routes import get_sync_stream
from routers.api.sync.stream import (
    EVENTS_PAGE_SIZE,
    SyncStreamStats,
    _DELETE_TYPE_ORDER,
    _generate_reset_stream,
    _stream_entity_type,
    generate_sync_stream,
)
from routers.immich_models import SyncEntityType, SyncRequestType, SyncStreamDto
from services.checkpoint_store import Checkpoint, CheckpointStore
from services.session_store import SessionStore
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_person_id,
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_face_id,
    uuid_to_gumnut_person_id,
)
from tests.unit.api.sync.conftest import (
    TEST_SESSION_UUID,
    TEST_UUID,
    collect_stream,
    create_mock_album_asset_data,
    create_mock_album_data,
    create_mock_asset_data,
    create_mock_entity_page,
    create_mock_exif_data,
    create_mock_face_data,
    create_mock_gumnut_client,
    create_mock_person_data,
    create_mock_session,
    create_mock_user,
    create_mock_event,
    create_mock_events_response,
)


class TestGenerateSyncStream:
    """Tests for generate_sync_stream function."""

    # -------------------------------------------------------------------------
    # Core behavior tests
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_always_streams_sync_complete(self):
        """SyncCompleteV1 is always streamed at the end."""
        mock_user = create_mock_user(datetime.now(timezone.utc))
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"
        assert events[0]["data"] == {}

    @pytest.mark.anyio
    async def test_event_format_includes_ack_with_cursor(self):
        """Each event includes an ack string with cursor for checkpointing.

        Ack format: "SyncEntityType|cursor|"
        """
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(user_updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        auth_event = events[0]
        assert "ack" in auth_event

        # Verify ack format: "SyncEntityType|cursor|"
        ack_parts = auth_event["ack"].split("|")
        assert len(ack_parts) == 3, (
            f"Expected 3 parts in ack, got {len(ack_parts)}: {auth_event['ack']}"
        )
        assert ack_parts[0] == "AuthUserV1"
        assert (
            ack_parts[1] == user_updated_at.isoformat()
        )  # cursor is updated_at for user entities
        assert ack_parts[2] == ""  # trailing empty string from trailing pipe

    @pytest.mark.anyio
    async def test_asset_event_ack_includes_cursor(self):
        """Asset events from events API include cursor in ack.

        Ack format: "SyncEntityType|cursor|"
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Set up event
        asset_data = create_mock_asset_data(updated_at)
        mock_event = create_mock_event(
            entity_type="asset",
            entity_id=asset_data.id,
            event_type="asset_created",
            created_at=updated_at,
            cursor="event_abc123",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])
        # Set up entity fetch
        mock_client.assets.list.return_value = create_mock_entity_page([asset_data])

        request = SyncStreamDto(types=[SyncRequestType.AssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        asset_event_output = events[0]
        assert asset_event_output["type"] == "AssetV1"

        # Verify ack format: "SyncEntityType|cursor|"
        ack_parts = asset_event_output["ack"].split("|")
        assert len(ack_parts) == 3, (
            f"Expected 3 parts in ack, got {len(ack_parts)}: {asset_event_output['ack']}"
        )
        assert ack_parts[0] == "AssetV1"
        assert ack_parts[1] == "event_abc123"  # cursor from event
        assert ack_parts[2] == ""  # trailing empty string from trailing pipe

    @pytest.mark.anyio
    async def test_closes_stream_on_exception(self):
        """Stream closes without yielding an error event when an exception occurs."""
        mock_client = Mock()
        mock_client.users.me.side_effect = Exception("API error")

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 0

    # -------------------------------------------------------------------------
    # User entity tests (special cases - not from events API)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_streams_auth_user_when_requested(self):
        """Auth user is streamed when AuthUsersV1 is requested."""
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(user_updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AuthUserV1"
        assert events[0]["data"]["email"] == "test@example.com"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_user_when_requested(self):
        """User is streamed when UsersV1 is requested."""
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(user_updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.UsersV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "UserV1"
        assert events[0]["data"]["email"] == "test@example.com"
        assert events[1]["type"] == "SyncCompleteV1"

    # -------------------------------------------------------------------------
    # Checkpoint/delta sync tests
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_skips_entity_when_checkpoint_matches(self):
        """User entity is skipped when checkpoint cursor matches updated_at."""
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        checkpoint_time = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(user_updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AuthUserV1,
            updated_at=checkpoint_time,
            cursor=user_updated_at.isoformat(),
        )
        checkpoint_map = {SyncEntityType.AuthUserV1: checkpoint}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_entity_when_user_updated_since_checkpoint(self):
        """User entity is re-streamed when updated_at differs from checkpoint cursor."""
        user_updated_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        old_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        checkpoint_time = datetime(2025, 1, 16, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(user_updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AuthUserV1,
            updated_at=checkpoint_time,
            cursor=old_updated_at.isoformat(),
        )
        checkpoint_map = {SyncEntityType.AuthUserV1: checkpoint}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AuthUserV1"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_entity_when_no_checkpoint(self):
        """User entity is streamed when no checkpoint exists."""
        user_updated_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(user_updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AuthUserV1"

    # -------------------------------------------------------------------------
    # Events API entity tests (events + entity fetch)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_streams_assets_when_requested(self):
        """Assets are streamed when AssetsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Set up event
        asset_data = create_mock_asset_data(updated_at)
        mock_event = create_mock_event(
            entity_type="asset",
            entity_id=asset_data.id,
            event_type="asset_created",
            created_at=updated_at,
            cursor="cursor_asset_1",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])
        mock_client.assets.list.return_value = create_mock_entity_page([asset_data])

        request = SyncStreamDto(types=[SyncRequestType.AssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetV1"
        assert events[0]["data"]["originalFileName"] == "test.jpg"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_albums_when_requested(self):
        """Albums are streamed when AlbumsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        album_data = create_mock_album_data(updated_at)
        mock_event = create_mock_event(
            entity_type="album",
            entity_id=album_data.id,
            event_type="album_created",
            created_at=updated_at,
            cursor="cursor_album_1",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])
        mock_client.albums.list.return_value = create_mock_entity_page([album_data])

        request = SyncStreamDto(types=[SyncRequestType.AlbumsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AlbumV1"
        assert events[0]["data"]["name"] == "Test Album"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_exif_when_requested(self):
        """EXIF data is streamed when AssetExifsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        exif_data = create_mock_exif_data(updated_at)
        # For exif, we need an asset with exif attached
        asset_with_exif = create_mock_asset_data(updated_at)
        asset_with_exif.id = exif_data.asset_id
        asset_with_exif.exif = exif_data

        mock_event = create_mock_event(
            entity_type="exif",
            entity_id=exif_data.asset_id,
            event_type="exif_created",
            created_at=updated_at,
            cursor="cursor_exif_1",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])
        mock_client.assets.list.return_value = create_mock_entity_page(
            [asset_with_exif]
        )

        request = SyncStreamDto(types=[SyncRequestType.AssetExifsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetExifV1"
        assert events[0]["data"]["city"] == "San Francisco"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_people_when_requested(self):
        """People are streamed when PeopleV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        person_data = create_mock_person_data(updated_at)
        mock_event = create_mock_event(
            entity_type="person",
            entity_id=person_data.id,
            event_type="person_created",
            created_at=updated_at,
            cursor="cursor_person_1",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])
        mock_client.people.list.return_value = create_mock_entity_page([person_data])

        request = SyncStreamDto(types=[SyncRequestType.PeopleV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "PersonV1"
        assert events[0]["data"]["name"] == "Test Person"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_faces_when_requested(self):
        """Faces are streamed when AssetFacesV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        face_data = create_mock_face_data(updated_at)
        mock_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_created",
            created_at=updated_at,
            cursor="cursor_face_1",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        request = SyncStreamDto(types=[SyncRequestType.AssetFacesV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetFaceV1"
        assert "boundingBoxX1" in events[0]["data"]
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_album_assets_when_requested(self):
        """Album-to-asset links are streamed when AlbumToAssetsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        album_asset_data = create_mock_album_asset_data(updated_at)
        mock_event = create_mock_event(
            entity_type="album_asset",
            entity_id=album_asset_data.id,
            event_type="album_asset_added",
            created_at=updated_at,
            cursor="cursor_album_asset_1",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])
        mock_client.album_assets.list.return_value = create_mock_entity_page(
            [album_asset_data]
        )

        request = SyncStreamDto(types=[SyncRequestType.AlbumToAssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AlbumToAssetV1"
        assert "albumId" in events[0]["data"]
        assert "assetId" in events[0]["data"]
        assert events[1]["type"] == "SyncCompleteV1"

        # Verify album_assets.list was called with correct IDs
        mock_client.album_assets.list.assert_called_once_with(
            ids=[album_asset_data.id], limit=1
        )

    @pytest.mark.anyio
    async def test_streams_album_asset_removed_event(self):
        """album_asset_removed events with payload produce AlbumToAssetDeleteV1."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        album_id = uuid_to_gumnut_album_id(TEST_UUID)
        asset_id = uuid_to_gumnut_asset_id(UUID("00000000-0000-0000-0000-000000000099"))
        mock_event = create_mock_event(
            entity_type="album_asset",
            entity_id="album_asset_some_id",
            event_type="album_asset_removed",
            created_at=updated_at,
            cursor="cursor_del_aa",
        )
        mock_event.payload = {"album_id": album_id, "asset_id": asset_id}
        mock_client.events.get.return_value = create_mock_events_response([mock_event])

        request = SyncStreamDto(types=[SyncRequestType.AlbumToAssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AlbumToAssetDeleteV1"
        assert events[0]["data"]["albumId"] == str(TEST_UUID)
        assert events[0]["data"]["assetId"] == str(
            UUID("00000000-0000-0000-0000-000000000099")
        )
        # Verify cursor is propagated in the ack string
        ack_parts = events[0]["ack"].split("|")
        assert ack_parts[0] == "AlbumToAssetDeleteV1"
        assert ack_parts[1] == "cursor_del_aa"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_skips_album_asset_removed_without_payload(self):
        """album_asset_removed events without payload are gracefully skipped."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        mock_event = create_mock_event(
            entity_type="album_asset",
            entity_id="album_asset_some_id",
            event_type="album_asset_removed",
            created_at=updated_at,
            cursor="cursor_del_aa",
        )
        mock_event.payload = None  # Old event before migration
        mock_client.events.get.return_value = create_mock_events_response([mock_event])

        request = SyncStreamDto(types=[SyncRequestType.AlbumToAssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        # Only SyncCompleteV1 — album_asset_removed without payload was skipped
        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"

    # -------------------------------------------------------------------------
    # Delete event tests
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_streams_asset_delete_event(self):
        """Asset delete events are converted to Immich AssetDeleteV1."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        asset_id = uuid_to_gumnut_asset_id(TEST_UUID)
        mock_event = create_mock_event(
            entity_type="asset",
            entity_id=asset_id,
            event_type="asset_deleted",
            created_at=updated_at,
            cursor="cursor_del_1",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])

        request = SyncStreamDto(types=[SyncRequestType.AssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetDeleteV1"
        assert "assetId" in events[0]["data"]
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_album_delete_event(self):
        """Album delete events are converted to Immich AlbumDeleteV1."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        album_id = uuid_to_gumnut_album_id(TEST_UUID)
        mock_event = create_mock_event(
            entity_type="album",
            entity_id=album_id,
            event_type="album_deleted",
            created_at=updated_at,
            cursor="cursor_del_2",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])

        request = SyncStreamDto(types=[SyncRequestType.AlbumsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AlbumDeleteV1"
        assert "albumId" in events[0]["data"]

    @pytest.mark.anyio
    async def test_streams_person_delete_event(self):
        """Person delete events are converted to Immich PersonDeleteV1."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        person_id = uuid_to_gumnut_person_id(TEST_UUID)
        mock_event = create_mock_event(
            entity_type="person",
            entity_id=person_id,
            event_type="person_deleted",
            created_at=updated_at,
            cursor="cursor_del_3",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])

        request = SyncStreamDto(types=[SyncRequestType.PeopleV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "PersonDeleteV1"
        assert "personId" in events[0]["data"]

    @pytest.mark.anyio
    async def test_streams_face_delete_event(self):
        """Face delete events are converted to Immich AssetFaceDeleteV1."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        face_id = uuid_to_gumnut_face_id(TEST_UUID)
        mock_event = create_mock_event(
            entity_type="face",
            entity_id=face_id,
            event_type="face_deleted",
            created_at=updated_at,
            cursor="cursor_del_4",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])

        request = SyncStreamDto(types=[SyncRequestType.AssetFacesV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetFaceDeleteV1"
        assert "assetFaceId" in events[0]["data"]

    @pytest.mark.anyio
    async def test_skips_exif_deleted_event(self):
        """exif_deleted events are silently skipped."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        mock_event = create_mock_event(
            entity_type="exif",
            entity_id="some-asset-id",
            event_type="exif_deleted",
            created_at=updated_at,
            cursor="cursor_del_5",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])

        request = SyncStreamDto(types=[SyncRequestType.AssetExifsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        # Only SyncCompleteV1 — exif_deleted was skipped
        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_skips_missing_entity(self):
        """Entity deleted between event and fetch is silently skipped."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        mock_event = create_mock_event(
            entity_type="asset",
            entity_id="nonexistent-asset-id",
            event_type="asset_created",
            created_at=updated_at,
            cursor="cursor_missing_1",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])
        # Entity not in fetch results — empty page
        mock_client.assets.list.return_value = create_mock_entity_page([])

        request = SyncStreamDto(types=[SyncRequestType.AssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        # Only SyncCompleteV1 — missing entity was skipped
        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_mixed_upsert_and_delete_events(self):
        """Upsert and delete events are processed in order."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        asset_data = create_mock_asset_data(updated_at)
        deleted_asset_id = uuid_to_gumnut_asset_id(
            UUID("00000000-0000-0000-0000-000000000099")
        )

        # First event: upsert, second event: delete
        mock_events = [
            create_mock_event(
                entity_type="asset",
                entity_id=asset_data.id,
                event_type="asset_created",
                created_at=updated_at,
                cursor="cursor_1",
            ),
            create_mock_event(
                entity_type="asset",
                entity_id=deleted_asset_id,
                event_type="asset_deleted",
                created_at=updated_at,
                cursor="cursor_2",
            ),
        ]
        mock_client.events.get.return_value = create_mock_events_response(mock_events)
        mock_client.assets.list.return_value = create_mock_entity_page([asset_data])

        request = SyncStreamDto(types=[SyncRequestType.AssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        # AssetV1 (upsert) + AssetDeleteV1 (delete) + SyncCompleteV1
        assert len(events) == 3
        assert events[0]["type"] == "AssetV1"
        assert events[1]["type"] == "AssetDeleteV1"
        assert events[2]["type"] == "SyncCompleteV1"


class TestGetSyncStreamEndpoint:
    """Tests for the get_sync_stream endpoint."""

    @pytest.mark.anyio
    async def test_returns_streaming_response_with_correct_media_type(self):
        """Endpoint returns StreamingResponse with jsonlines media type."""
        from fastapi.responses import StreamingResponse

        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        mock_request = Mock()
        mock_request.state.session_token = None

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_checkpoint_store.get_all.return_value = []

        mock_session_store = AsyncMock(spec=SessionStore)
        mock_session_store.get_by_id.return_value = None

        request = SyncStreamDto(types=[])

        result = await get_sync_stream(
            request=request,
            http_request=mock_request,
            gumnut_client=mock_client,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        assert isinstance(result, StreamingResponse)
        assert result.media_type == "application/jsonlines+json"

    @pytest.mark.anyio
    async def test_loads_checkpoints_when_session_token_present(self):
        """Checkpoints are loaded from store when session token is valid."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        # Create checkpoint with matching updated_at cursor to cause auth user to be skipped
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AuthUserV1,
            updated_at=datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc),
            cursor=updated_at.isoformat(),
        )
        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_checkpoint_store.get_all.return_value = [checkpoint]

        mock_session_store = AsyncMock(spec=SessionStore)
        mock_session_store.get_by_id.return_value = create_mock_session()

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])

        result = await get_sync_stream(
            request=request,
            http_request=mock_request,
            gumnut_client=mock_client,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # Verify checkpoint store was called with correct session UUID
        mock_checkpoint_store.get_all.assert_called_once_with(TEST_SESSION_UUID)

        # Consume stream and verify auth user was skipped due to checkpoint
        events = []
        async for chunk in result.body_iterator:
            line = bytes(chunk).decode() if not isinstance(chunk, str) else chunk
            events.append(json.loads(line.strip()))

        # Only SyncCompleteV1 (auth user skipped because checkpoint exists)
        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_pending_sync_reset_sends_only_reset_event(self):
        """When session has isPendingSyncReset, only SyncResetV1 is sent."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)

        mock_session_store = AsyncMock(spec=SessionStore)
        mock_session_store.get_by_id.return_value = create_mock_session(
            is_pending_sync_reset=True
        )

        request = SyncStreamDto(
            types=[SyncRequestType.AuthUsersV1, SyncRequestType.AssetsV1]
        )

        result = await get_sync_stream(
            request=request,
            http_request=mock_request,
            gumnut_client=Mock(),
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        events = []
        async for chunk in result.body_iterator:
            line = bytes(chunk).decode() if not isinstance(chunk, str) else chunk
            events.append(json.loads(line.strip()))

        assert len(events) == 1
        assert events[0]["type"] == "SyncResetV1"
        assert events[0]["data"] == {}
        assert events[0]["ack"] == "SyncResetV1|reset|"

        mock_checkpoint_store.get_all.assert_not_called()
        mock_checkpoint_store.delete_all.assert_not_called()

    @pytest.mark.anyio
    async def test_request_reset_clears_checkpoints(self):
        """When request.reset=True, all checkpoints are cleared before streaming."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)

        mock_session_store = AsyncMock(spec=SessionStore)
        mock_session_store.get_by_id.return_value = create_mock_session()

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1], reset=True)

        result = await get_sync_stream(
            request=request,
            http_request=mock_request,
            gumnut_client=mock_client,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        mock_checkpoint_store.delete_all.assert_called_once_with(TEST_SESSION_UUID)
        mock_checkpoint_store.get_all.assert_not_called()

        events = []
        async for chunk in result.body_iterator:
            line = bytes(chunk).decode() if not isinstance(chunk, str) else chunk
            events.append(json.loads(line.strip()))

        assert len(events) == 2
        assert events[0]["type"] == "AuthUserV1"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_request_reset_without_session_does_not_clear(self):
        """When request.reset=True but no session, checkpoints are not cleared."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        mock_request = Mock()
        mock_request.state.session_token = None

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)
        mock_session_store = AsyncMock(spec=SessionStore)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1], reset=True)

        await get_sync_stream(
            request=request,
            http_request=mock_request,
            gumnut_client=mock_client,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        mock_checkpoint_store.delete_all.assert_not_called()
        mock_checkpoint_store.get_all.assert_not_called()


class TestFacePersonIdOverride:
    """Face events should use causally-consistent person_id, not current state.

    The sync stream fetches the CURRENT state of entities, not their state at
    event time. When a face_created event is processed, the adapter fetches
    the face's current state from the API. If face clustering has since assigned
    the face to a person, the current state includes person_id — but the
    person_created event may not be in this sync cycle.

    The Immich mobile client enforces FK constraints in its local SQLite DB,
    so receiving a face with a personId for a person that was never delivered
    causes: SqliteException(787) FOREIGN KEY constraint failed in
    updateAssetFacesV1.
    """

    @pytest.mark.anyio
    async def test_face_created_nulls_person_id_from_current_state(self):
        """face_created events null out person_id even if current state has one.

        Scenario:
        1. Face detection ran → face_created event (face had person_id=NULL)
        2. Face clustering ran → assigned face to person P1 (face now has person_id=P1)
        3. Sync starts — the face_created event is within the window
        4. Adapter fetches face's CURRENT state → gets person_id=P1
        5. Adapter nulls person_id because face_created always means no person

        The face_updated event from clustering will deliver the correct
        person_id in the same or a future sync cycle.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Face entity's CURRENT state has person_id set
        # (clustering assigned it after detection)
        face_data = create_mock_face_data(updated_at)
        assert face_data.person_id is not None, "Test setup: face should have person_id"

        # But the EVENT is face_created (from when the face had no person)
        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_created",
            created_at=updated_at,
            cursor="cursor_face_1",
        )

        mock_client.events.get.return_value = create_mock_events_response([face_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="face",
            sync_entity_type=SyncEntityType.AssetFaceV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        assert len(results) == 1
        json_line, count = results[0]
        event_data = json.loads(json_line.strip())

        assert event_data["type"] == "AssetFaceV1"
        assert event_data["data"]["personId"] is None, (
            "face_created event should null out person_id to avoid referencing "
            "a person that may not be in this sync cycle"
        )

        # Verify the original entity wasn't mutated (model_copy creates a new instance)
        assert face_data.person_id is not None, (
            "Original face entity should not be mutated by stream processing"
        )

    @pytest.mark.anyio
    async def test_face_created_does_not_reference_undelivered_person(self):
        """face_created events don't reference people outside the sync window.

        Scenario:
        1. Face detection created face F1 (person_id=NULL) → face_created event
        2. Face clustering created person P1, assigned F1 → person_created event
           + face_updated event, but BOTH happened after sync_started_at
        3. Sync starts with created_at_lt = sync_started_at
        4. People stream: person_created event is AFTER sync_started_at → not returned
        5. Face stream: face_created event is BEFORE sync_started_at → returned
        6. Adapter fetches F1 current state → person_id=P1
        7. Adapter nulls person_id on face_created → no FK violation

        The face_updated event from clustering will deliver person_id=P1
        in the same or a future sync cycle.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Face's CURRENT state has person_id (assigned by clustering after detection)
        face_data = create_mock_face_data(updated_at)

        # face_created event exists (from face detection, before sync window boundary)
        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_created",
            created_at=updated_at,
            cursor="cursor_face_1",
        )

        # No person events in this sync window — the person was created after
        # sync_started_at, so its event is excluded by the created_at_lt filter
        def mock_events_get(**kwargs: Any) -> Any:
            entity_types = kwargs.get("entity_types", "")
            if entity_types == "person":
                return create_mock_events_response([])
            elif entity_types == "face":
                return create_mock_events_response([face_event])
            return create_mock_events_response([])

        mock_client.events.get.side_effect = mock_events_get
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        request = SyncStreamDto(
            types=[SyncRequestType.PeopleV1, SyncRequestType.AssetFacesV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        # Collect what was streamed
        face_person_refs = set()

        for event in events:
            if event["type"] == "AssetFaceV1":
                pid = event["data"].get("personId")
                if pid:
                    face_person_refs.add(pid)

        # After fix: face_created events have person_id nulled out, so no
        # orphaned references to undelivered people.
        assert not face_person_refs, (
            "face_created events should not reference any person, but found "
            f"person references: {face_person_refs}"
        )

    @pytest.mark.anyio
    async def test_face_references_person_in_sync_stream_when_both_present(self):
        """When both person and face events are in the sync window, no orphan.

        This is the happy path: face clustering events (person_created +
        face_updated) are both within the sync window, so the person is
        delivered before the face.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Person and face data
        person_data = create_mock_person_data(updated_at)
        face_data = create_mock_face_data(updated_at)
        # face_data.person_id already points to person with TEST_UUID

        person_event = create_mock_event(
            entity_type="person",
            entity_id=person_data.id,
            event_type="person_created",
            created_at=updated_at,
            cursor="cursor_person_1",
        )
        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_updated",
            created_at=updated_at,
            cursor="cursor_face_1",
        )

        def mock_events_get(**kwargs):
            entity_types = kwargs.get("entity_types", "")
            if entity_types == "person":
                return create_mock_events_response([person_event])
            elif entity_types == "face":
                return create_mock_events_response([face_event])
            return create_mock_events_response([])

        mock_client.events.get.side_effect = mock_events_get
        mock_client.people.list.return_value = create_mock_entity_page([person_data])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        request = SyncStreamDto(
            types=[SyncRequestType.PeopleV1, SyncRequestType.AssetFacesV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        streamed_person_ids = set()
        face_person_refs = set()

        for event in events:
            if event["type"] == "PersonV1":
                streamed_person_ids.add(event["data"]["id"])
            elif event["type"] == "AssetFaceV1":
                pid = event["data"].get("personId")
                if pid:
                    face_person_refs.add(pid)

        # Happy path: all face person references are satisfied
        orphaned_refs = face_person_refs - streamed_person_ids
        assert not orphaned_refs, (
            f"Face references unsatisfied person IDs: {orphaned_refs}"
        )

    @pytest.mark.anyio
    async def test_face_updated_uses_person_id_from_payload(self):
        """face_updated events use person_id from event payload, not current state.

        When the event payload carries a person_id, the adapter should use
        that value instead of the entity's current person_id. This ensures
        the sync stream delivers the causally-consistent value from event
        time, not a potentially stale current value.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Face entity's CURRENT state has person_id=P1 (from a later clustering run)
        face_data = create_mock_face_data(updated_at)
        original_person_id = face_data.person_id

        # But the event payload carries person_id=P2 (from when the event was recorded)
        different_uuid = UUID("00000000-0000-0000-0000-000000000002")
        payload_person_id = uuid_to_gumnut_person_id(different_uuid)
        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_updated",
            created_at=updated_at,
            cursor="cursor_face_1",
            payload={"person_id": payload_person_id},
        )

        mock_client.events.get.return_value = create_mock_events_response([face_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="face",
            sync_entity_type=SyncEntityType.AssetFaceV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        assert len(results) == 1
        json_line, count = results[0]
        event_data = json.loads(json_line.strip())

        # Converter maps person_id through safe_uuid_from_person_id, so the
        # output should be the UUID form of the payload person_id, not the original
        expected_uuid = str(different_uuid)
        actual_person_id = event_data["data"]["personId"]
        assert actual_person_id == expected_uuid, (
            f"face_updated should use person_id from payload ({expected_uuid}), "
            f"not current entity state, but got {actual_person_id}"
        )

        # Verify the original entity wasn't mutated
        assert face_data.person_id == original_person_id, (
            "Original face entity should not be mutated by stream processing"
        )

    @pytest.mark.anyio
    async def test_face_updated_without_payload_uses_current_state(self):
        """Legacy face_updated events (no payload) fall through with current state.

        Events recorded before the payload fix don't have person_id in
        the payload. These should pass through with the entity's current
        person_id unchanged.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        face_data = create_mock_face_data(updated_at)

        # Legacy event — no payload
        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_updated",
            created_at=updated_at,
            cursor="cursor_face_1",
        )

        mock_client.events.get.return_value = create_mock_events_response([face_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="face",
            sync_entity_type=SyncEntityType.AssetFaceV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        assert len(results) == 1
        json_line, count = results[0]
        event_data = json.loads(json_line.strip())

        # Legacy event: should use entity's current person_id
        assert face_data.person_id is not None
        expected_uuid = str(safe_uuid_from_person_id(face_data.person_id))
        assert event_data["data"]["personId"] == expected_uuid, (
            "Legacy face_updated (no payload) should use entity's current person_id"
        )

    @pytest.mark.anyio
    async def test_face_updated_payload_with_null_person_id(self):
        """face_updated with payload person_id=null unassigns the face from its person."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Face entity's CURRENT state has a person_id
        face_data = create_mock_face_data(updated_at)
        assert face_data.person_id is not None

        # Event payload says person_id is now None (unassigned)
        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_updated",
            created_at=updated_at,
            cursor="cursor_face_1",
            payload={"person_id": None},
        )

        mock_client.events.get.return_value = create_mock_events_response([face_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="face",
            sync_entity_type=SyncEntityType.AssetFaceV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        assert len(results) == 1
        json_line, count = results[0]
        event_data = json.loads(json_line.strip())

        assert event_data["data"]["personId"] is None, (
            "face_updated with payload person_id=None should null out personId"
        )

    @pytest.mark.anyio
    async def test_face_updated_with_empty_payload_dict_uses_current_state(self):
        """face_updated with empty payload {} (no person_id key) uses current state.

        A producer bug might send an empty payload dict. Since person_id
        is not present in the payload, the handler should not trigger and
        the entity's current person_id should pass through unchanged.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        face_data = create_mock_face_data(updated_at)

        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_updated",
            created_at=updated_at,
            cursor="cursor_face_1",
            payload={},
        )

        mock_client.events.get.return_value = create_mock_events_response([face_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="face",
            sync_entity_type=SyncEntityType.AssetFaceV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        assert len(results) == 1
        json_line, count = results[0]
        event_data = json.loads(json_line.strip())

        # Empty payload: should use entity's current person_id
        assert face_data.person_id is not None
        expected_uuid = str(safe_uuid_from_person_id(face_data.person_id))
        assert event_data["data"]["personId"] == expected_uuid, (
            "face_updated with empty payload {} should use entity's current person_id"
        )


class TestGenerateResetStream:
    """Tests for _generate_reset_stream helper function."""

    @pytest.mark.anyio
    async def test_generates_single_reset_event(self):
        """Reset stream contains only SyncResetV1 with correct format."""
        events = []
        async for line in _generate_reset_stream():
            events.append(json.loads(line.strip()))

        assert len(events) == 1
        assert events[0]["type"] == "SyncResetV1"
        assert events[0]["data"] == {}
        assert events[0]["ack"] == "SyncResetV1|reset|"


class TestStreamEntityTypePagination:
    """Tests for cursor-based pagination in _stream_entity_type function."""

    @pytest.mark.anyio
    async def test_first_call_uses_checkpoint_cursor(self):
        """First API call uses cursor from checkpoint as after_cursor."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Return empty response so we don't loop
        mock_client.events.get.return_value = create_mock_events_response([])

        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AssetV1,
            updated_at=updated_at,
            cursor="event_checkpoint_cursor",
        )

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="asset",
            sync_entity_type=SyncEntityType.AssetV1,
            owner_id=str(TEST_UUID),
            checkpoint=checkpoint,
            sync_started_at=sync_started_at,
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        mock_client.events.get.assert_called_once_with(
            created_at_lt=sync_started_at,
            entity_types="asset",
            limit=EVENTS_PAGE_SIZE,
            after_cursor="event_checkpoint_cursor",
        )

    @pytest.mark.anyio
    async def test_first_call_without_checkpoint_omits_after_cursor(self):
        """First API call without checkpoint omits after_cursor parameter."""
        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        mock_user = create_mock_user(sync_started_at)
        mock_client = create_mock_gumnut_client(mock_user)

        mock_client.events.get.return_value = create_mock_events_response([])

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="asset",
            sync_entity_type=SyncEntityType.AssetV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        mock_client.events.get.assert_called_once_with(
            created_at_lt=sync_started_at,
            entity_types="asset",
            limit=EVENTS_PAGE_SIZE,
        )

    @pytest.mark.anyio
    async def test_pagination_uses_last_event_cursor_for_next_page(self):
        """Subsequent calls use cursor from last event of previous page."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Create first page with has_more=True
        first_page_events = []
        for i in range(EVENTS_PAGE_SIZE):
            asset_uuid = UUID(f"00000000-0000-0000-0000-{i:012d}")
            asset_id = uuid_to_gumnut_asset_id(asset_uuid)
            first_page_events.append(
                create_mock_event(
                    entity_type="asset",
                    entity_id=asset_id,
                    event_type="asset_created",
                    created_at=updated_at,
                    cursor=f"cursor_{i}",
                )
            )

        # Create matching asset data for first page, keyed by ID
        first_page_assets_by_id: dict[str, Mock] = {}
        for i in range(EVENTS_PAGE_SIZE):
            asset_uuid = UUID(f"00000000-0000-0000-0000-{i:012d}")
            asset_data = create_mock_asset_data(updated_at)
            asset_data.id = uuid_to_gumnut_asset_id(asset_uuid)
            first_page_assets_by_id[asset_data.id] = asset_data

        # Create second page with 1 event
        second_asset_uuid = UUID("00000000-0000-0000-0000-000000000500")
        second_asset_id = uuid_to_gumnut_asset_id(second_asset_uuid)
        second_page_event = create_mock_event(
            entity_type="asset",
            entity_id=second_asset_id,
            event_type="asset_created",
            created_at=updated_at,
            cursor="cursor_500",
        )
        second_asset_data = create_mock_asset_data(updated_at)
        second_asset_data.id = second_asset_id

        # Set up mock responses
        first_response = create_mock_events_response(first_page_events, has_more=True)
        second_response = create_mock_events_response([second_page_event])

        mock_client.events.get.side_effect = [first_response, second_response]

        # Mock assets.list to return entities matching the requested IDs.
        # With FETCH_BATCH_SIZE chunking, assets.list is called multiple times
        # per event page.
        all_assets_by_id = {
            **first_page_assets_by_id,
            second_asset_id: second_asset_data,
        }

        def mock_assets_list(**kwargs: Any) -> Mock:
            ids = kwargs.get("ids", [])
            matching = [all_assets_by_id[id_] for id_ in ids if id_ in all_assets_by_id]
            return create_mock_entity_page(matching)

        mock_client.assets.list.side_effect = mock_assets_list

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="asset",
            sync_entity_type=SyncEntityType.AssetV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        assert len(results) == EVENTS_PAGE_SIZE + 1

        # Verify second call used cursor from last event of first page
        calls = mock_client.events.get.call_args_list
        assert len(calls) == 2

        second_call = calls[1]
        assert second_call == call(
            created_at_lt=sync_started_at,
            entity_types="asset",
            limit=EVENTS_PAGE_SIZE,
            after_cursor=f"cursor_{EVENTS_PAGE_SIZE - 1}",
        )

    @pytest.mark.anyio
    async def test_stops_when_has_more_is_false(self):
        """Pagination stops when has_more is False, even with full page."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Create exactly EVENTS_PAGE_SIZE events but has_more=False
        page_events = []
        assets_by_id: dict[str, Mock] = {}
        for i in range(EVENTS_PAGE_SIZE):
            asset_uuid = UUID(f"00000000-0000-0000-0000-{i:012d}")
            asset_id = uuid_to_gumnut_asset_id(asset_uuid)
            page_events.append(
                create_mock_event(
                    entity_type="asset",
                    entity_id=asset_id,
                    event_type="asset_created",
                    created_at=updated_at,
                    cursor=f"cursor_{i}",
                )
            )
            asset_data = create_mock_asset_data(updated_at)
            asset_data.id = asset_id
            assets_by_id[asset_id] = asset_data

        # has_more=False — should not make a second call
        mock_client.events.get.return_value = create_mock_events_response(
            page_events, has_more=False
        )

        # Mock assets.list to return entities matching the requested IDs
        def mock_assets_list(**kwargs: Any) -> Mock:
            ids = kwargs.get("ids", [])
            matching = [assets_by_id[id_] for id_ in ids if id_ in assets_by_id]
            return create_mock_entity_page(matching)

        mock_client.assets.list.side_effect = mock_assets_list

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="asset",
            sync_entity_type=SyncEntityType.AssetV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        assert len(results) == EVENTS_PAGE_SIZE
        # Only one API call — no second page fetch
        mock_client.events.get.assert_called_once()


class TestUpsertsBeforeDeletes:
    """Tests for two-phase sync stream ordering: upserts first, deletes last.

    The sync stream buffers delete events during phase 1 (upserts) and yields
    them in reverse FK dependency order during phase 2 (deletes). This prevents
    FK constraint violations in the mobile client when a parent entity is
    deleted before a child entity referencing it is updated.
    """

    @pytest.mark.anyio
    async def test_person_delete_yielded_after_face_upsert(self):
        """Core bug scenario: person_deleted must come after face_updated.

        When a person is deleted and their faces are reassigned, the face_updated
        event references the old (deleted) person_id. If person_deleted arrives
        first, the mobile client deletes the person locally, and the subsequent
        face insert fails with an FK constraint violation.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        person_data = create_mock_person_data(updated_at)
        face_data = create_mock_face_data(updated_at)

        deleted_person_id = uuid_to_gumnut_person_id(
            UUID("00000000-0000-0000-0000-000000000099")
        )

        # Person events: person_created then person_deleted
        person_events = [
            create_mock_event(
                entity_type="person",
                entity_id=person_data.id,
                event_type="person_created",
                created_at=updated_at,
                cursor="cursor_p1",
            ),
            create_mock_event(
                entity_type="person",
                entity_id=deleted_person_id,
                event_type="person_deleted",
                created_at=updated_at,
                cursor="cursor_p2",
            ),
        ]

        # Face events: face_updated referencing the deleted person
        face_events = [
            create_mock_event(
                entity_type="face",
                entity_id=face_data.id,
                event_type="face_updated",
                created_at=updated_at,
                cursor="cursor_f1",
                payload={"person_id": deleted_person_id},
            ),
        ]

        def mock_events_get(**kwargs: Any) -> Any:
            entity_types = kwargs.get("entity_types", "")
            if entity_types == "person":
                return create_mock_events_response(person_events)
            elif entity_types == "face":
                return create_mock_events_response(face_events)
            return create_mock_events_response([])

        mock_client.events.get.side_effect = mock_events_get
        mock_client.people.list.return_value = create_mock_entity_page([person_data])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        request = SyncStreamDto(
            types=[SyncRequestType.PeopleV1, SyncRequestType.AssetFacesV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        event_types = [e["type"] for e in events]

        # PersonV1 (upsert) and AssetFaceV1 (upsert) must come before
        # PersonDeleteV1 (delete)
        person_upsert_idx = event_types.index("PersonV1")
        face_upsert_idx = event_types.index("AssetFaceV1")
        person_delete_idx = event_types.index("PersonDeleteV1")

        assert person_upsert_idx < person_delete_idx
        assert face_upsert_idx < person_delete_idx

    @pytest.mark.anyio
    async def test_all_upserts_before_any_deletes(self):
        """ALL upserts must come before ANY deletes across entity types."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        asset_data = create_mock_asset_data(updated_at)
        person_data = create_mock_person_data(updated_at)

        deleted_asset_id = uuid_to_gumnut_asset_id(
            UUID("00000000-0000-0000-0000-000000000088")
        )
        deleted_person_id = uuid_to_gumnut_person_id(
            UUID("00000000-0000-0000-0000-000000000099")
        )

        asset_events = [
            create_mock_event(
                entity_type="asset",
                entity_id=asset_data.id,
                event_type="asset_created",
                created_at=updated_at,
                cursor="cursor_a1",
            ),
            create_mock_event(
                entity_type="asset",
                entity_id=deleted_asset_id,
                event_type="asset_deleted",
                created_at=updated_at,
                cursor="cursor_a2",
            ),
        ]

        person_events = [
            create_mock_event(
                entity_type="person",
                entity_id=person_data.id,
                event_type="person_created",
                created_at=updated_at,
                cursor="cursor_p1",
            ),
            create_mock_event(
                entity_type="person",
                entity_id=deleted_person_id,
                event_type="person_deleted",
                created_at=updated_at,
                cursor="cursor_p2",
            ),
        ]

        def mock_events_get(**kwargs: Any) -> Any:
            entity_types = kwargs.get("entity_types", "")
            if entity_types == "asset":
                return create_mock_events_response(asset_events)
            elif entity_types == "person":
                return create_mock_events_response(person_events)
            return create_mock_events_response([])

        mock_client.events.get.side_effect = mock_events_get
        mock_client.assets.list.return_value = create_mock_entity_page([asset_data])
        mock_client.people.list.return_value = create_mock_entity_page([person_data])

        request = SyncStreamDto(
            types=[SyncRequestType.AssetsV1, SyncRequestType.PeopleV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        event_types = [e["type"] for e in events if e["type"] != "SyncCompleteV1"]

        # Find the boundary between upserts and deletes
        delete_types = {"AssetDeleteV1", "PersonDeleteV1"}
        upsert_types = {"AssetV1", "PersonV1"}

        first_delete_idx = next(
            (i for i, t in enumerate(event_types) if t in delete_types), None
        )
        last_upsert_idx = next(
            (
                len(event_types) - 1 - i
                for i, t in enumerate(reversed(event_types))
                if t in upsert_types
            ),
            None,
        )

        assert first_delete_idx is not None, "Expected delete events"
        assert last_upsert_idx is not None, "Expected upsert events"
        assert last_upsert_idx < first_delete_idx, (
            f"Last upsert at {last_upsert_idx} must be before first delete "
            f"at {first_delete_idx}. Event types: {event_types}"
        )

    @pytest.mark.anyio
    async def test_deletes_in_reverse_fk_order(self):
        """Delete events are yielded in reverse FK order: faces before persons before assets."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        deleted_asset_id = uuid_to_gumnut_asset_id(
            UUID("00000000-0000-0000-0000-000000000011")
        )
        deleted_person_id = uuid_to_gumnut_person_id(
            UUID("00000000-0000-0000-0000-000000000022")
        )
        deleted_face_id = uuid_to_gumnut_face_id(
            UUID("00000000-0000-0000-0000-000000000033")
        )

        asset_events = [
            create_mock_event(
                entity_type="asset",
                entity_id=deleted_asset_id,
                event_type="asset_deleted",
                created_at=updated_at,
                cursor="cursor_a1",
            ),
        ]
        person_events = [
            create_mock_event(
                entity_type="person",
                entity_id=deleted_person_id,
                event_type="person_deleted",
                created_at=updated_at,
                cursor="cursor_p1",
            ),
        ]
        face_events = [
            create_mock_event(
                entity_type="face",
                entity_id=deleted_face_id,
                event_type="face_deleted",
                created_at=updated_at,
                cursor="cursor_f1",
            ),
        ]

        def mock_events_get(**kwargs: Any) -> Any:
            entity_types = kwargs.get("entity_types", "")
            if entity_types == "asset":
                return create_mock_events_response(asset_events)
            elif entity_types == "person":
                return create_mock_events_response(person_events)
            elif entity_types == "face":
                return create_mock_events_response(face_events)
            return create_mock_events_response([])

        mock_client.events.get.side_effect = mock_events_get

        request = SyncStreamDto(
            types=[
                SyncRequestType.AssetsV1,
                SyncRequestType.PeopleV1,
                SyncRequestType.AssetFacesV1,
            ]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        delete_types = [
            e["type"]
            for e in events
            if e["type"].endswith("DeleteV1") and e["type"] != "SyncCompleteV1"
        ]

        # Reverse FK order: faces → persons → assets
        assert delete_types == [
            "AssetFaceDeleteV1",
            "PersonDeleteV1",
            "AssetDeleteV1",
        ]

    @pytest.mark.anyio
    async def test_only_deletes_no_upserts(self):
        """When only delete events exist, they are yielded in reverse FK order."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        deleted_asset_id = uuid_to_gumnut_asset_id(
            UUID("00000000-0000-0000-0000-000000000011")
        )

        asset_events = [
            create_mock_event(
                entity_type="asset",
                entity_id=deleted_asset_id,
                event_type="asset_deleted",
                created_at=updated_at,
                cursor="cursor_a1",
            ),
        ]

        mock_client.events.get.return_value = create_mock_events_response(asset_events)

        request = SyncStreamDto(types=[SyncRequestType.AssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        event_types = [e["type"] for e in events]
        assert "AssetDeleteV1" in event_types
        assert event_types[-1] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_empty_delete_buffer(self):
        """Stream works normally with only upserts and no deletes."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        asset_data = create_mock_asset_data(updated_at)

        asset_events = [
            create_mock_event(
                entity_type="asset",
                entity_id=asset_data.id,
                event_type="asset_created",
                created_at=updated_at,
                cursor="cursor_a1",
            ),
        ]

        mock_client.events.get.return_value = create_mock_events_response(asset_events)
        mock_client.assets.list.return_value = create_mock_entity_page([asset_data])

        request = SyncStreamDto(types=[SyncRequestType.AssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        event_types = [e["type"] for e in events]
        assert event_types == ["AssetV1", "SyncCompleteV1"]

    @pytest.mark.anyio
    async def test_delete_chronological_order_within_type(self):
        """Multiple deletes of the same type maintain chronological (cursor) order."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        person_id_1 = uuid_to_gumnut_person_id(
            UUID("00000000-0000-0000-0000-000000000001")
        )
        person_id_2 = uuid_to_gumnut_person_id(
            UUID("00000000-0000-0000-0000-000000000002")
        )
        person_id_3 = uuid_to_gumnut_person_id(
            UUID("00000000-0000-0000-0000-000000000003")
        )

        person_events = [
            create_mock_event(
                entity_type="person",
                entity_id=person_id_1,
                event_type="person_deleted",
                created_at=updated_at,
                cursor="cursor_1",
            ),
            create_mock_event(
                entity_type="person",
                entity_id=person_id_2,
                event_type="person_deleted",
                created_at=updated_at,
                cursor="cursor_2",
            ),
            create_mock_event(
                entity_type="person",
                entity_id=person_id_3,
                event_type="person_deleted",
                created_at=updated_at,
                cursor="cursor_3",
            ),
        ]

        def mock_events_get(**kwargs: Any) -> Any:
            entity_types = kwargs.get("entity_types", "")
            if entity_types == "person":
                return create_mock_events_response(person_events)
            return create_mock_events_response([])

        mock_client.events.get.side_effect = mock_events_get

        request = SyncStreamDto(types=[SyncRequestType.PeopleV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        delete_events = [e for e in events if e["type"] == "PersonDeleteV1"]
        ack_cursors = [e["ack"] for e in delete_events]

        # Cursors should maintain chronological order
        assert ack_cursors == [
            "PersonDeleteV1|cursor_1|",
            "PersonDeleteV1|cursor_2|",
            "PersonDeleteV1|cursor_3|",
        ]


class TestDeleteTypeOrderCompleteness:
    """Ensure _DELETE_TYPE_ORDER covers all delete SyncEntityTypes the adapter handles."""

    def test_all_handled_delete_types_in_delete_type_order(self):
        """Every delete SyncEntityType produced by _make_delete_sync_event must
        appear in _DELETE_TYPE_ORDER so buffered deletes are emitted in a
        deterministic, FK-safe order."""
        handled_delete_types = {
            SyncEntityType.AssetDeleteV1,
            SyncEntityType.AlbumDeleteV1,
            SyncEntityType.PersonDeleteV1,
            SyncEntityType.AssetFaceDeleteV1,
            SyncEntityType.AlbumToAssetDeleteV1,
        }
        missing = handled_delete_types - set(_DELETE_TYPE_ORDER)
        assert not missing, (
            f"Delete types handled by _make_delete_sync_event but missing "
            f"from _DELETE_TYPE_ORDER: {missing}"
        )


class TestAlbumCoverPayloadOverride:
    """Tests for album_updated event payload override of album_cover_asset_id.

    Same pattern as face_updated/person_id: the event payload carries the
    causally-consistent album_cover_asset_id from event time. The adapter
    should prefer this over the entity's current state, which is computed
    at fetch time and may reference an asset outside the sync window.
    """

    async def _stream_album_event(
        self,
        album_data: Any,
        album_event: Any,
    ) -> dict[str, Any]:
        """Stream a single album event and return the parsed sync event data."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)
        mock_client.events.get.return_value = create_mock_events_response([album_event])
        mock_client.albums.list.return_value = create_mock_entity_page([album_data])

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="album",
            sync_entity_type=SyncEntityType.AlbumV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc),
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        assert len(results) == 1
        json_line, _count = results[0]
        return json.loads(json_line.strip())

    @pytest.mark.anyio
    async def test_album_updated_uses_cover_from_payload(self):
        """album_updated events use album_cover_asset_id from event payload, not current state."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

        # Album entity's CURRENT state has cover = asset_A (from a later asset addition)
        current_cover_id = uuid_to_gumnut_asset_id(
            UUID("00000000-0000-0000-0000-000000000001")
        )
        album_data = create_mock_album_data(
            updated_at, album_cover_asset_id=current_cover_id
        )

        # But the event payload carries cover = asset_B (from when the event was recorded)
        payload_cover_id = uuid_to_gumnut_asset_id(
            UUID("00000000-0000-0000-0000-000000000002")
        )
        album_event = create_mock_event(
            entity_type="album",
            entity_id=album_data.id,
            event_type="album_updated",
            created_at=updated_at,
            cursor="cursor_album_1",
            payload={"album_cover_asset_id": payload_cover_id},
        )

        event_data = await self._stream_album_event(album_data, album_event)

        expected_uuid = str(safe_uuid_from_asset_id(payload_cover_id))
        actual_cover = event_data["data"]["thumbnailAssetId"]
        assert actual_cover == expected_uuid, (
            f"album_updated should use album_cover_asset_id from payload ({expected_uuid}), "
            f"not current entity state, but got {actual_cover}"
        )

        # Verify the original entity wasn't mutated
        assert album_data.album_cover_asset_id == current_cover_id, (
            "Original album entity should not be mutated by stream processing"
        )

    @pytest.mark.anyio
    async def test_album_updated_without_payload_uses_current_state(self):
        """Legacy album_updated events (no payload) fall through with current state."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

        current_cover_id = uuid_to_gumnut_asset_id(TEST_UUID)
        album_data = create_mock_album_data(
            updated_at, album_cover_asset_id=current_cover_id
        )

        # Legacy event — no payload
        album_event = create_mock_event(
            entity_type="album",
            entity_id=album_data.id,
            event_type="album_updated",
            created_at=updated_at,
            cursor="cursor_album_1",
        )

        event_data = await self._stream_album_event(album_data, album_event)

        expected_uuid = str(safe_uuid_from_asset_id(current_cover_id))
        assert event_data["data"]["thumbnailAssetId"] == expected_uuid, (
            "Legacy album_updated (no payload) should use entity's current album_cover_asset_id"
        )

    @pytest.mark.anyio
    async def test_album_updated_payload_with_null_cover(self):
        """album_updated with payload album_cover_asset_id=null sets cover to null."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

        # Album entity's CURRENT state has a cover (asset was added after event)
        current_cover_id = uuid_to_gumnut_asset_id(TEST_UUID)
        album_data = create_mock_album_data(
            updated_at, album_cover_asset_id=current_cover_id, asset_count=0
        )

        # Event payload says cover was null at event time (album was empty then)
        album_event = create_mock_event(
            entity_type="album",
            entity_id=album_data.id,
            event_type="album_updated",
            created_at=updated_at,
            cursor="cursor_album_1",
            payload={"album_cover_asset_id": None},
        )

        event_data = await self._stream_album_event(album_data, album_event)

        assert event_data["data"]["thumbnailAssetId"] is None, (
            "album_updated with payload album_cover_asset_id=None should null out thumbnailAssetId"
        )


class TestFacePayloadOverrideDeletedPerson:
    """Tests for face_updated payload override when the referenced person is deleted.

    When a face_updated event's payload carries a person_id for a person that
    was deleted after the event was recorded, the adapter should null out the
    person_id rather than streaming a reference to a non-existent entity.

    Without this fix, the Immich mobile client gets a permanent FK constraint
    violation (SqliteException 787) in updateAssetFacesV1 because it tries to
    insert a face referencing a person that was never delivered.
    """

    @pytest.mark.anyio
    async def test_face_updated_nulls_person_id_when_person_deleted(self):
        """face_updated payload person_id is nulled when the person returns 404.

        Scenario (fresh sync, no checkpoints):
        1. face_updated event recorded with person_id=P1 in payload
        2. Person P1 is deleted after the event was recorded
        3. Sync starts — adapter fetches person P1 → 404 (not in fetch results)
        4. face_updated event's payload overrides person_id to P1
        5. P1 was never streamed → must null out person_id to avoid FK violation
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # The deleted person's ID — will return 404 (not in people.list results)
        deleted_person_uuid = UUID("00000000-0000-0000-0000-000000000099")
        deleted_person_id = uuid_to_gumnut_person_id(deleted_person_uuid)

        # Face entity's CURRENT state has a different person_id (reassigned)
        face_data = create_mock_face_data(updated_at)
        current_person_id = face_data.person_id
        assert current_person_id != deleted_person_id

        # The face_updated event payload references the deleted person
        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_updated",
            created_at=updated_at,
            cursor="cursor_face_1",
            payload={"person_id": deleted_person_id},
        )

        # Person events: person_created then person_deleted for the same person
        person_events = [
            create_mock_event(
                entity_type="person",
                entity_id=deleted_person_id,
                event_type="person_created",
                created_at=updated_at,
                cursor="cursor_p1",
            ),
            create_mock_event(
                entity_type="person",
                entity_id=deleted_person_id,
                event_type="person_deleted",
                created_at=updated_at,
                cursor="cursor_p2",
            ),
        ]

        def mock_events_get(**kwargs: Any) -> Any:
            entity_types = kwargs.get("entity_types", "")
            if entity_types == "person":
                return create_mock_events_response(person_events)
            elif entity_types == "face":
                return create_mock_events_response([face_event])
            return create_mock_events_response([])

        mock_client.events.get.side_effect = mock_events_get
        # Person fetch returns empty — person was deleted
        mock_client.people.list.return_value = create_mock_entity_page([])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        request = SyncStreamDto(
            types=[SyncRequestType.PeopleV1, SyncRequestType.AssetFacesV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        face_events = [e for e in events if e["type"] == "AssetFaceV1"]
        assert len(face_events) == 1

        assert face_events[0]["data"]["personId"] is None, (
            "face_updated should null person_id when the referenced person "
            "was deleted (404 on fetch) to avoid FK constraint violation"
        )

    @pytest.mark.anyio
    async def test_face_updated_keeps_person_id_when_person_synced_prior_cycle(self):
        """face_updated payload person_id is kept when person was synced in a prior cycle.

        Scenario (incremental sync, person checkpoint exists):
        1. Person P1 was synced in a prior cycle (client has it locally)
        2. face_updated event recorded with person_id=P1 in payload
        3. Person P1 is NOT modified in this sync window (no person events)
        4. The adapter should keep person_id=P1 — the client already has it

        This ensures the fix for deleted persons doesn't break incremental syncs
        where the person exists but simply wasn't modified in this window.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Person P1 — exists, was synced before, not modified in this window
        person_uuid = UUID("00000000-0000-0000-0000-000000000002")
        payload_person_id = uuid_to_gumnut_person_id(person_uuid)

        # Face entity's CURRENT state has a different person_id
        face_data = create_mock_face_data(updated_at)

        # face_updated event with payload person_id = P1
        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_updated",
            created_at=updated_at,
            cursor="cursor_face_1",
            payload={"person_id": payload_person_id},
        )

        mock_client.events.get.return_value = create_mock_events_response([face_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        # Incremental sync: person checkpoint exists (person type was synced before)
        checkpoint_map = {
            SyncEntityType.PersonV1: Checkpoint(
                entity_type=SyncEntityType.PersonV1,
                cursor="prior_cursor",
                updated_at=updated_at,
            ),
        }

        stats = SyncStreamStats()
        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="face",
            sync_entity_type=SyncEntityType.AssetFaceV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=stats,
            checkpoint_map=checkpoint_map,
        ):
            results.append(item)

        assert len(results) == 1
        json_line, count = results[0]
        event_data = json.loads(json_line.strip())

        # Person was synced in a prior cycle — person_id should be preserved
        expected_uuid = str(person_uuid)
        assert event_data["data"]["personId"] == expected_uuid, (
            "face_updated should keep payload person_id when the person was "
            "synced in a prior cycle (checkpoint exists for PersonV1)"
        )

    @pytest.mark.anyio
    async def test_face_reassignment_sequence_after_person_deletion(self):
        """Full reassignment sequence: person deleted, face unassigned, face gets new person.

        Reproduces the exact production timeline:
        1. face_updated with payload person_id=P1 (clustering assigned P1)
        2. person_deleted for P1
        3. face_updated with payload=null (unassignment from deleted P1)
        4. face_updated with payload person_id=P2 (reassigned to new person)

        The face's current state has person_id=P2. After sync:
        - Event 1 should have person_id nulled (P1 is deleted/404)
        - Event 3 should use current state (P2) since payload is null
        - Event 4 should have person_id=P2 from payload
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Person P1 (deleted) and Person P2 (alive)
        p1_uuid = UUID("00000000-0000-0000-0000-000000000001")
        p2_uuid = UUID("00000000-0000-0000-0000-000000000002")
        p1_id = uuid_to_gumnut_person_id(p1_uuid)
        p2_id = uuid_to_gumnut_person_id(p2_uuid)

        # Face's current state has person_id = P2 (reassigned)
        face_data = FaceResponse(
            id=uuid_to_gumnut_face_id(TEST_UUID),
            asset_id=uuid_to_gumnut_asset_id(TEST_UUID),
            person_id=p2_id,
            bounding_box={"x": 100, "y": 100, "w": 50, "h": 50},
            created_at=updated_at,
            updated_at=updated_at,
        )

        # Person P2 exists
        p2_data = Mock()
        p2_data.id = p2_id
        p2_data.name = "Person Two"
        p2_data.is_favorite = False
        p2_data.is_hidden = False
        p2_data.created_at = updated_at
        p2_data.updated_at = updated_at

        # Events in chronological order
        person_events = [
            create_mock_event(
                entity_type="person",
                entity_id=p1_id,
                event_type="person_created",
                created_at=updated_at,
                cursor="cursor_p1",
            ),
            create_mock_event(
                entity_type="person",
                entity_id=p2_id,
                event_type="person_created",
                created_at=updated_at,
                cursor="cursor_p2",
            ),
            create_mock_event(
                entity_type="person",
                entity_id=p1_id,
                event_type="person_deleted",
                created_at=updated_at,
                cursor="cursor_p3",
            ),
        ]

        face_events = [
            # Event 1: clustering assigned face to P1
            create_mock_event(
                entity_type="face",
                entity_id=face_data.id,
                event_type="face_updated",
                created_at=updated_at,
                cursor="cursor_f1",
                payload={"person_id": p1_id},
            ),
            # Event 2: face unassigned from deleted P1 (null payload)
            create_mock_event(
                entity_type="face",
                entity_id=face_data.id,
                event_type="face_updated",
                created_at=updated_at,
                cursor="cursor_f2",
                payload=None,
            ),
            # Event 3: face reassigned to P2
            create_mock_event(
                entity_type="face",
                entity_id=face_data.id,
                event_type="face_updated",
                created_at=updated_at,
                cursor="cursor_f3",
                payload={"person_id": p2_id},
            ),
        ]

        def mock_events_get(**kwargs: Any) -> Any:
            entity_types = kwargs.get("entity_types", "")
            if entity_types == "person":
                return create_mock_events_response(person_events)
            elif entity_types == "face":
                return create_mock_events_response(face_events)
            return create_mock_events_response([])

        mock_client.events.get.side_effect = mock_events_get
        # Person P1 is deleted (404), P2 exists
        mock_client.people.list.return_value = create_mock_entity_page([p2_data])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        request = SyncStreamDto(
            types=[SyncRequestType.PeopleV1, SyncRequestType.AssetFacesV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        face_events_out = [e for e in events if e["type"] == "AssetFaceV1"]
        assert len(face_events_out) == 3, (
            f"Expected 3 face events, got {len(face_events_out)}"
        )

        # Event 1: payload person_id=P1, but P1 is deleted → should be nulled
        assert face_events_out[0]["data"]["personId"] is None, (
            "First face_updated (payload person_id=P1) should null person_id "
            "because P1 was deleted (404 on fetch)"
        )

        # Event 2: null payload → uses current state (P2)
        assert face_events_out[1]["data"]["personId"] == str(p2_uuid), (
            "Second face_updated (null payload) should use current state person_id (P2)"
        )

        # Event 3: payload person_id=P2, P2 exists → should keep P2
        assert face_events_out[2]["data"]["personId"] == str(p2_uuid), (
            "Third face_updated (payload person_id=P2) should keep P2 "
            "because P2 was streamed in this cycle"
        )

    @pytest.mark.anyio
    async def test_multiple_faces_referencing_same_deleted_person(self):
        """Multiple faces with payload person_id referencing the same deleted person.

        All faces should have their person_id nulled, not just the first one.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        deleted_person_uuid = UUID("00000000-0000-0000-0000-000000000099")
        deleted_person_id = uuid_to_gumnut_person_id(deleted_person_uuid)

        # Two different faces, both referencing the deleted person in payload
        face1_uuid = UUID("00000000-0000-0000-0000-000000000011")
        face2_uuid = UUID("00000000-0000-0000-0000-000000000022")

        face1_data = FaceResponse(
            id=uuid_to_gumnut_face_id(face1_uuid),
            asset_id=uuid_to_gumnut_asset_id(TEST_UUID),
            person_id=None,  # current state: unassigned
            bounding_box={"x": 10, "y": 10, "w": 50, "h": 50},
            created_at=updated_at,
            updated_at=updated_at,
        )
        face2_data = FaceResponse(
            id=uuid_to_gumnut_face_id(face2_uuid),
            asset_id=uuid_to_gumnut_asset_id(TEST_UUID),
            person_id=None,  # current state: unassigned
            bounding_box={"x": 100, "y": 100, "w": 50, "h": 50},
            created_at=updated_at,
            updated_at=updated_at,
        )

        face_events = [
            create_mock_event(
                entity_type="face",
                entity_id=face1_data.id,
                event_type="face_updated",
                created_at=updated_at,
                cursor="cursor_f1",
                payload={"person_id": deleted_person_id},
            ),
            create_mock_event(
                entity_type="face",
                entity_id=face2_data.id,
                event_type="face_updated",
                created_at=updated_at,
                cursor="cursor_f2",
                payload={"person_id": deleted_person_id},
            ),
        ]

        person_events = [
            create_mock_event(
                entity_type="person",
                entity_id=deleted_person_id,
                event_type="person_created",
                created_at=updated_at,
                cursor="cursor_p1",
            ),
            create_mock_event(
                entity_type="person",
                entity_id=deleted_person_id,
                event_type="person_deleted",
                created_at=updated_at,
                cursor="cursor_p2",
            ),
        ]

        def mock_events_get(**kwargs: Any) -> Any:
            entity_types = kwargs.get("entity_types", "")
            if entity_types == "person":
                return create_mock_events_response(person_events)
            elif entity_types == "face":
                return create_mock_events_response(face_events)
            return create_mock_events_response([])

        mock_client.events.get.side_effect = mock_events_get
        mock_client.people.list.return_value = create_mock_entity_page([])
        mock_client.faces.list.return_value = create_mock_entity_page(
            [face1_data, face2_data]
        )

        request = SyncStreamDto(
            types=[SyncRequestType.PeopleV1, SyncRequestType.AssetFacesV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        face_events_out = [e for e in events if e["type"] == "AssetFaceV1"]
        assert len(face_events_out) == 2

        for i, face_event in enumerate(face_events_out):
            assert face_event["data"]["personId"] is None, (
                f"Face {i + 1} should have person_id nulled when the referenced "
                f"person was deleted (404)"
            )


class TestAlbumPayloadOverrideDeletedAsset:
    """Tests for album_updated payload override when the referenced asset is deleted.

    Same pattern as face/person: when an album_updated event's payload carries
    an album_cover_asset_id for an asset that was deleted after the event was
    recorded, the adapter should null out the cover to avoid FK violations.
    """

    @pytest.mark.anyio
    async def test_album_updated_nulls_cover_when_asset_deleted(self):
        """album_updated payload album_cover_asset_id is nulled when asset returns 404."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # The deleted asset's ID — will return 404
        deleted_asset_uuid = UUID("00000000-0000-0000-0000-000000000088")
        deleted_asset_id = uuid_to_gumnut_asset_id(deleted_asset_uuid)

        # Album's current state has no cover (asset was deleted)
        album_data = create_mock_album_data(
            updated_at, album_cover_asset_id=None, asset_count=0
        )

        # But the event payload references the deleted asset as the cover
        album_event = create_mock_event(
            entity_type="album",
            entity_id=album_data.id,
            event_type="album_updated",
            created_at=updated_at,
            cursor="cursor_a1",
            payload={"album_cover_asset_id": deleted_asset_id},
        )

        asset_events = [
            create_mock_event(
                entity_type="asset",
                entity_id=deleted_asset_id,
                event_type="asset_created",
                created_at=updated_at,
                cursor="cursor_asset1",
            ),
            create_mock_event(
                entity_type="asset",
                entity_id=deleted_asset_id,
                event_type="asset_deleted",
                created_at=updated_at,
                cursor="cursor_asset2",
            ),
        ]

        album_events = [album_event]

        def mock_events_get(**kwargs: Any) -> Any:
            entity_types = kwargs.get("entity_types", "")
            if entity_types == "asset":
                return create_mock_events_response(asset_events)
            elif entity_types == "album":
                return create_mock_events_response(album_events)
            return create_mock_events_response([])

        mock_client.events.get.side_effect = mock_events_get
        # Asset was deleted — returns empty from fetch
        mock_client.assets.list.return_value = create_mock_entity_page([])
        mock_client.albums.list.return_value = create_mock_entity_page([album_data])

        request = SyncStreamDto(
            types=[SyncRequestType.AssetsV1, SyncRequestType.AlbumsV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        album_events_out = [e for e in events if e["type"] == "AlbumV1"]
        assert len(album_events_out) == 1

        assert album_events_out[0]["data"]["thumbnailAssetId"] is None, (
            "album_updated should null album_cover_asset_id when the referenced "
            "asset was deleted (404 on fetch)"
        )
