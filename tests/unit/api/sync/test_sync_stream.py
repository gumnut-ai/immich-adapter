"""Tests for sync stream generation and endpoint."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, call
from uuid import UUID

import pytest

from routers.api.sync import (
    _generate_reset_stream,
    _get_entity_id_for_pagination,
    _stream_entity_type,
    generate_sync_stream,
    get_sync_stream,
)
from routers.immich_models import SyncEntityType, SyncRequestType, SyncStreamDto
from services.checkpoint_store import Checkpoint, CheckpointStore
from services.session_store import SessionStore
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id
from tests.unit.api.sync.conftest import (
    TEST_SESSION_UUID,
    TEST_UUID,
    AlbumAssetEventPayload,
    AlbumEventPayload,
    AssetEventPayload,
    ExifEventPayload,
    FaceEventPayload,
    PersonEventPayload,
    collect_stream,
    create_mock_album_asset_data,
    create_mock_album_data,
    create_mock_asset_data,
    create_mock_event,
    create_mock_exif_data,
    create_mock_face_data,
    create_mock_gumnut_client,
    create_mock_person_data,
    create_mock_session,
    create_mock_user,
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
    async def test_event_format_includes_ack_with_entity_id(self):
        """Each event includes an ack string with entity_id for checkpointing.

        Ack format: "SyncEntityType|timestamp|entity_id|"
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

        # Verify ack format: "SyncEntityType|timestamp|entity_id|"
        ack_parts = auth_event["ack"].split("|")
        assert len(ack_parts) == 4, (
            f"Expected 4 parts in ack, got {len(ack_parts)}: {auth_event['ack']}"
        )
        assert ack_parts[0] == "AuthUserV1"
        assert ack_parts[1] == user_updated_at.isoformat()
        assert ack_parts[2] == mock_user.id  # entity_id should be the user ID
        assert ack_parts[3] == ""  # trailing empty string from trailing pipe

    @pytest.mark.anyio
    async def test_asset_event_ack_includes_entity_id(self):
        """Asset events from events API include entity_id in ack.

        Ack format: "SyncEntityType|timestamp|entity_id|"
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        asset_data = create_mock_asset_data(updated_at)
        asset_event = create_mock_event(AssetEventPayload, asset_data)

        events_response = Mock()
        events_response.data = [asset_event]
        mock_client.events.get.return_value = events_response

        request = SyncStreamDto(types=[SyncRequestType.AssetsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        asset_event_output = events[0]
        assert asset_event_output["type"] == "AssetV1"

        # Verify ack format: "SyncEntityType|timestamp|entity_id|"
        ack_parts = asset_event_output["ack"].split("|")
        assert len(ack_parts) == 4, (
            f"Expected 4 parts in ack, got {len(ack_parts)}: {asset_event_output['ack']}"
        )
        assert ack_parts[0] == "AssetV1"
        assert ack_parts[1] == updated_at.isoformat()
        assert ack_parts[2] == asset_data.id  # entity_id should be the asset ID
        assert ack_parts[3] == ""  # trailing empty string from trailing pipe

    @pytest.mark.anyio
    async def test_streams_error_on_exception(self):
        """Error event is streamed when an exception occurs."""
        mock_client = Mock()
        mock_client.users.me.side_effect = Exception("API error")

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 1
        assert events[0]["type"] == "Error"
        assert "message" in events[0]["data"]

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
    async def test_skips_entity_when_not_updated_since_checkpoint(self):
        """Entity is skipped when checkpoint is newer than updated_at."""
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        checkpoint_time = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(user_updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AuthUserV1,
            last_synced_at=checkpoint_time,
            updated_at=checkpoint_time,
        )
        checkpoint_map = {SyncEntityType.AuthUserV1: checkpoint}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_entity_when_updated_after_checkpoint(self):
        """Entity is streamed when updated_at is after checkpoint."""
        user_updated_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        checkpoint_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(user_updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AuthUserV1,
            last_synced_at=checkpoint_time,
            updated_at=checkpoint_time,
        )
        checkpoint_map = {SyncEntityType.AuthUserV1: checkpoint}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AuthUserV1"

    # -------------------------------------------------------------------------
    # Events API entity tests (in processing order)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_streams_assets_when_requested(self):
        """Assets are streamed when AssetsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        asset_data = create_mock_asset_data(updated_at)
        asset_event = create_mock_event(AssetEventPayload, asset_data)

        events_response = Mock()
        events_response.data = [asset_event]
        mock_client.events.get.return_value = events_response

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
        album_event = create_mock_event(AlbumEventPayload, album_data)

        events_response = Mock()
        events_response.data = [album_event]
        mock_client.events.get.return_value = events_response

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
    async def test_streams_album_assets_when_requested(self):
        """Album-to-asset mappings are streamed when AlbumToAssetsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        album_asset_data = create_mock_album_asset_data(updated_at)
        album_asset_event = create_mock_event(AlbumAssetEventPayload, album_asset_data)

        events_response = Mock()
        events_response.data = [album_asset_event]
        mock_client.events.get.return_value = events_response

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

    @pytest.mark.anyio
    async def test_streams_exif_when_requested(self):
        """EXIF data is streamed when AssetExifsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        exif_data = create_mock_exif_data(updated_at)
        exif_event = create_mock_event(ExifEventPayload, exif_data)

        events_response = Mock()
        events_response.data = [exif_event]
        mock_client.events.get.return_value = events_response

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
        person_event = create_mock_event(PersonEventPayload, person_data)

        events_response = Mock()
        events_response.data = [person_event]
        mock_client.events.get.return_value = events_response

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
        face_event = create_mock_event(FaceEventPayload, face_data)

        events_response = Mock()
        events_response.data = [face_event]
        mock_client.events.get.return_value = events_response

        request = SyncStreamDto(types=[SyncRequestType.AssetFacesV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetFaceV1"
        assert "boundingBoxX1" in events[0]["data"]
        assert events[1]["type"] == "SyncCompleteV1"


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

        # Create checkpoint that will cause auth user to be skipped
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AuthUserV1,
            last_synced_at=datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc),
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

        # Only SyncCompleteV1 (auth user skipped because checkpoint is newer)
        assert len(events) == 1
        assert events[0]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_pending_sync_reset_sends_only_reset_event(self):
        """When session has isPendingSyncReset, only SyncResetV1 is sent.

        This matches immich behavior: when a reset is pending, the server
        sends SyncResetV1 and ends the stream immediately. No other entity
        types are sent, regardless of what was requested.
        """
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)

        # Session has pending reset flag set
        mock_session_store = AsyncMock(spec=SessionStore)
        mock_session_store.get_by_id.return_value = create_mock_session(
            is_pending_sync_reset=True
        )

        # Request multiple entity types - none should be returned
        request = SyncStreamDto(
            types=[SyncRequestType.AuthUsersV1, SyncRequestType.AssetsV1]
        )

        result = await get_sync_stream(
            request=request,
            http_request=mock_request,
            gumnut_client=Mock(),  # Should not be called
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # Consume stream
        events = []
        async for chunk in result.body_iterator:
            line = bytes(chunk).decode() if not isinstance(chunk, str) else chunk
            events.append(json.loads(line.strip()))

        # Only SyncResetV1 should be returned
        assert len(events) == 1
        assert events[0]["type"] == "SyncResetV1"
        assert events[0]["data"] == {}
        assert events[0]["ack"] == "SyncResetV1|reset"

        # Checkpoint store should not be called (no loading/clearing)
        mock_checkpoint_store.get_all.assert_not_called()
        mock_checkpoint_store.delete_all.assert_not_called()

    @pytest.mark.anyio
    async def test_request_reset_clears_checkpoints(self):
        """When request.reset=True, all checkpoints are cleared before streaming.

        This triggers a full sync from the beginning. The client sends reset=True
        when it wants to start fresh (e.g., user manually requested full re-sync).
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        mock_checkpoint_store = AsyncMock(spec=CheckpointStore)

        mock_session_store = AsyncMock(spec=SessionStore)
        mock_session_store.get_by_id.return_value = create_mock_session()

        # Request with reset=True
        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1], reset=True)

        result = await get_sync_stream(
            request=request,
            http_request=mock_request,
            gumnut_client=mock_client,
            checkpoint_store=mock_checkpoint_store,
            session_store=mock_session_store,
        )

        # Verify checkpoints were deleted
        mock_checkpoint_store.delete_all.assert_called_once_with(TEST_SESSION_UUID)

        # Verify checkpoints were NOT loaded (since they were cleared)
        mock_checkpoint_store.get_all.assert_not_called()

        # Consume stream and verify auth user is returned (full sync)
        events = []
        async for chunk in result.body_iterator:
            line = bytes(chunk).decode() if not isinstance(chunk, str) else chunk
            events.append(json.loads(line.strip()))

        # AuthUserV1 should be streamed (no checkpoint to skip it)
        assert len(events) == 2
        assert events[0]["type"] == "AuthUserV1"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_request_reset_without_session_does_not_clear(self):
        """When request.reset=True but no session, checkpoints are not cleared.

        This handles the edge case where someone calls the endpoint without
        a valid session token. No error is raised, but no clearing occurs.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        mock_request = Mock()
        mock_request.state.session_token = None  # No session

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

        # No checkpoint operations should occur without a session
        mock_checkpoint_store.delete_all.assert_not_called()
        mock_checkpoint_store.get_all.assert_not_called()


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
        assert events[0]["ack"] == "SyncResetV1|reset"


class TestGetEntityIdForPagination:
    """Tests for _get_entity_id_for_pagination function."""

    def test_exif_event_returns_asset_id(self):
        """Exif events use asset_id as the entity ID for pagination."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        exif_data = create_mock_exif_data(updated_at)
        exif_event = create_mock_event(ExifEventPayload, exif_data)

        entity_id = _get_entity_id_for_pagination(exif_event)

        assert entity_id == exif_data.asset_id

    def test_asset_event_returns_id(self):
        """Asset events use id as the entity ID for pagination."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        asset_data = create_mock_asset_data(updated_at)
        asset_event = create_mock_event(AssetEventPayload, asset_data)

        entity_id = _get_entity_id_for_pagination(asset_event)

        assert entity_id == asset_data.id

    def test_album_event_returns_id(self):
        """Album events use id as the entity ID for pagination."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        album_data = create_mock_album_data(updated_at)
        album_event = create_mock_event(AlbumEventPayload, album_data)

        entity_id = _get_entity_id_for_pagination(album_event)

        assert entity_id == album_data.id

    def test_album_asset_event_returns_id(self):
        """Album asset events use id as the entity ID for pagination."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        album_asset_data = create_mock_album_asset_data(updated_at)
        album_asset_event = create_mock_event(AlbumAssetEventPayload, album_asset_data)

        entity_id = _get_entity_id_for_pagination(album_asset_event)

        assert entity_id == album_asset_data.id

    def test_person_event_returns_id(self):
        """Person events use id as the entity ID for pagination."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        person_data = create_mock_person_data(updated_at)
        person_event = create_mock_event(PersonEventPayload, person_data)

        entity_id = _get_entity_id_for_pagination(person_event)

        assert entity_id == person_data.id

    def test_face_event_returns_id(self):
        """Face events use id as the entity ID for pagination."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        face_data = create_mock_face_data(updated_at)
        face_event = create_mock_event(FaceEventPayload, face_data)

        entity_id = _get_entity_id_for_pagination(face_event)

        assert entity_id == face_data.id


class TestStreamEntityTypePagination:
    """Tests for keyset pagination in _stream_entity_type function."""

    @pytest.mark.anyio
    async def test_first_call_uses_checkpoint_entity_id(self):
        """First API call uses last_entity_id from checkpoint as starting_after_id."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        checkpoint_entity_id = "asset_checkpoint123"

        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Return empty response so we don't loop
        events_response = Mock()
        events_response.data = []
        mock_client.events.get.return_value = events_response

        checkpoint = Checkpoint(
            entity_type=SyncEntityType.AssetV1,
            last_synced_at=updated_at,
            updated_at=updated_at,
            last_entity_id=checkpoint_entity_id,
        )

        # Consume the generator
        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="asset",
            sync_entity_type=SyncEntityType.AssetV1,
            owner_id=str(TEST_UUID),
            checkpoint=checkpoint,
            sync_started_at=sync_started_at,
        ):
            results.append(item)

        # Verify the API was called with starting_after_id from checkpoint
        mock_client.events.get.assert_called_once_with(
            updated_at_gte=updated_at,
            updated_at_lt=sync_started_at,
            entity_types="asset",
            limit=500,
            starting_after_id=checkpoint_entity_id,
        )

    @pytest.mark.anyio
    async def test_first_call_without_checkpoint_uses_none(self):
        """First API call without checkpoint uses None for starting_after_id."""
        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        mock_user = create_mock_user(sync_started_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Return empty response so we don't loop
        events_response = Mock()
        events_response.data = []
        mock_client.events.get.return_value = events_response

        # Consume the generator
        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="asset",
            sync_entity_type=SyncEntityType.AssetV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
        ):
            results.append(item)

        # Verify the API was called with starting_after_id=None
        mock_client.events.get.assert_called_once_with(
            updated_at_gte=None,
            updated_at_lt=sync_started_at,
            entity_types="asset",
            limit=500,
            starting_after_id=None,
        )

    @pytest.mark.anyio
    async def test_pagination_uses_last_event_for_next_page(self):
        """Subsequent calls use updated_at and entity_id from last event."""

        updated_at_1 = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        updated_at_2 = datetime(2025, 1, 15, 11, 0, 0, tzinfo=timezone.utc)
        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        mock_user = create_mock_user(updated_at_1)
        mock_client = create_mock_gumnut_client(mock_user)

        # Create 500 assets for first page (triggers pagination)
        first_page_assets = []
        last_asset_id = None
        for i in range(500):
            asset_data = create_mock_asset_data(updated_at_1)
            # Generate valid Gumnut IDs using unique UUIDs
            asset_uuid = UUID(f"00000000-0000-0000-0000-{i:012d}")
            asset_data.id = uuid_to_gumnut_asset_id(asset_uuid)
            if i == 499:
                last_asset_id = asset_data.id
            asset_event = create_mock_event(AssetEventPayload, asset_data)
            first_page_assets.append(asset_event)

        # Create second page with 1 asset
        second_page_asset = create_mock_asset_data(updated_at_2)
        second_asset_uuid = UUID("00000000-0000-0000-0000-000000000500")
        second_page_asset.id = uuid_to_gumnut_asset_id(second_asset_uuid)
        second_page_event = create_mock_event(AssetEventPayload, second_page_asset)

        # Setup mock responses
        first_response = Mock()
        first_response.data = first_page_assets
        second_response = Mock()
        second_response.data = [second_page_event]

        mock_client.events.get.side_effect = [first_response, second_response]

        # Consume the generator
        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="asset",
            sync_entity_type=SyncEntityType.AssetV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
        ):
            results.append(item)

        # Verify we got 501 events
        assert len(results) == 501

        # Verify second call used cursor from last event of first page
        calls = mock_client.events.get.call_args_list
        assert len(calls) == 2

        # Second call should use the updated_at and id from the last asset
        second_call = calls[1]
        assert second_call == call(
            updated_at_gte=updated_at_1,  # from last event of page 1
            updated_at_lt=sync_started_at,
            entity_types="asset",
            limit=500,
            starting_after_id=last_asset_id,  # from last event of page 1
        )

    @pytest.mark.anyio
    async def test_identical_timestamps_handled_by_entity_id(self):
        """Entities with identical timestamps are properly paginated via entity_id."""

        same_timestamp = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        mock_user = create_mock_user(same_timestamp)
        mock_client = create_mock_gumnut_client(mock_user)

        # Create first page of 500 assets all with same timestamp
        first_page_assets = []
        last_asset_id = None
        for i in range(500):
            asset_data = create_mock_asset_data(same_timestamp)
            # Generate valid Gumnut IDs using unique UUIDs
            asset_uuid = UUID(f"00000000-0000-0000-0000-{i:012d}")
            asset_data.id = uuid_to_gumnut_asset_id(asset_uuid)
            if i == 499:
                last_asset_id = asset_data.id
            asset_event = create_mock_event(AssetEventPayload, asset_data)
            first_page_assets.append(asset_event)

        # Second page - more assets with SAME timestamp
        second_page_assets = []
        for i in range(500, 502):
            asset_data = create_mock_asset_data(same_timestamp)
            asset_uuid = UUID(f"00000000-0000-0000-0000-{i:012d}")
            asset_data.id = uuid_to_gumnut_asset_id(asset_uuid)
            asset_event = create_mock_event(AssetEventPayload, asset_data)
            second_page_assets.append(asset_event)

        # Setup mock responses
        first_response = Mock()
        first_response.data = first_page_assets
        second_response = Mock()
        second_response.data = second_page_assets

        mock_client.events.get.side_effect = [first_response, second_response]

        # Consume the generator
        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="asset",
            sync_entity_type=SyncEntityType.AssetV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
        ):
            results.append(item)

        # Should get all 502 entities despite same timestamp
        assert len(results) == 502

        # Verify second call uses same timestamp but with entity_id cursor
        calls = mock_client.events.get.call_args_list
        assert len(calls) == 2

        second_call = calls[1]
        # Key assertion: updated_at_gte is same timestamp, but starting_after_id
        # allows us to skip already-seen entities
        assert second_call == call(
            updated_at_gte=same_timestamp,
            updated_at_lt=sync_started_at,
            entity_types="asset",
            limit=500,
            starting_after_id=last_asset_id,  # Last entity from first page
        )

    @pytest.mark.anyio
    async def test_exif_pagination_uses_asset_id(self):
        """Exif events use asset_id for pagination instead of id."""

        updated_at_1 = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        updated_at_2 = datetime(2025, 1, 15, 11, 0, 0, tzinfo=timezone.utc)
        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        mock_user = create_mock_user(updated_at_1)
        mock_client = create_mock_gumnut_client(mock_user)

        # First page of 500 exif events
        first_page_exifs = []
        last_asset_id = None
        for i in range(500):
            exif_data = create_mock_exif_data(updated_at_1)
            # Generate valid Gumnut asset IDs using unique UUIDs
            asset_uuid = UUID(f"00000000-0000-0000-0000-{i:012d}")
            exif_data.asset_id = uuid_to_gumnut_asset_id(asset_uuid)
            if i == 499:
                last_asset_id = exif_data.asset_id
            exif_event = create_mock_event(ExifEventPayload, exif_data)
            first_page_exifs.append(exif_event)

        # Second page with one more exif
        second_exif = create_mock_exif_data(updated_at_2)
        second_asset_uuid = UUID("00000000-0000-0000-0000-000000000500")
        second_exif.asset_id = uuid_to_gumnut_asset_id(second_asset_uuid)
        second_event = create_mock_event(ExifEventPayload, second_exif)

        # Setup mock responses
        first_response = Mock()
        first_response.data = first_page_exifs
        second_response = Mock()
        second_response.data = [second_event]

        mock_client.events.get.side_effect = [first_response, second_response]

        # Consume the generator
        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="exif",
            sync_entity_type=SyncEntityType.AssetExifV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
        ):
            results.append(item)

        # Should get all 501 exif events
        assert len(results) == 501

        # Verify second call uses asset_id from last exif event
        calls = mock_client.events.get.call_args_list
        second_call = calls[1]
        assert second_call == call(
            updated_at_gte=updated_at_1,
            updated_at_lt=sync_started_at,
            entity_types="exif",
            limit=500,
            starting_after_id=last_asset_id,  # asset_id from last exif
        )
