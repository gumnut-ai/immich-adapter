"""Tests for sync stream generation, endpoint, reset, and pagination."""

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock, call
from uuid import UUID

import pytest

from routers.api.sync.routes import get_sync_stream
from routers.api.sync.converters import gumnut_album_to_sync_album_user_v1
from routers.api.sync.fk_integrity import SyncStreamStats
from routers.api.sync.stream import (
    EVENTS_PAGE_SIZE,
    _stream_entity_type,
    generate_reset_stream,
    generate_sync_stream,
)
from routers.immich_models import (
    AlbumUserRole,
    SyncEntityType,
    SyncRequestType,
    SyncStreamDto,
)
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_face_id,
    uuid_to_gumnut_person_id,
)
from services.checkpoint_store import Checkpoint, CheckpointStore
from services.session_store import SessionStore
from tests.unit.api.sync.conftest import (
    TEST_SESSION_UUID,
    TEST_UUID,
    collect_stream,
    create_mock_album_asset_data,
    create_mock_album_data,
    create_mock_asset_data,
    create_mock_entity_page,
    create_mock_event,
    create_mock_events_response,
    create_mock_face_data,
    create_mock_metadata_data,
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
        mock_user = create_mock_user(datetime.now(timezone.utc))
        mock_client = create_mock_gumnut_client(mock_user)
        # Simulate an error during event fetching inside the generator
        mock_client.events.get = AsyncMock(side_effect=Exception("API error"))

        request = SyncStreamDto(
            types=[SyncRequestType.AssetsV1],
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AuthUserV1"
        assert events[0]["data"]["email"] == "test@example.com"
        # Quota mirrors the user's storage caps (same source as /api/users/me)
        assert events[0]["data"]["quotaSizeInBytes"] == 100 * 1000**3
        assert events[0]["data"]["quotaUsageInBytes"] == 5 * 1000**3
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_auth_user_quota_none_coerces_usage_to_zero(self):
        """A user missing storage values → no cap / 0 usage on the sync auth user.

        quotaUsageInBytes is a required int on SyncAuthUserV1, so a None upstream
        usage (rollout) coerces to 0; quotaSizeInBytes stays None (unlimited).
        """
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(user_updated_at)
        # What the SDK yields when an older Gumnut API omits the storage fields
        mock_user.storage_limit_bytes = None
        mock_user.storage_used_bytes = None
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.AuthUsersV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        assert events[0]["type"] == "AuthUserV1"
        assert events[0]["data"]["quotaSizeInBytes"] is None
        assert events[0]["data"]["quotaUsageInBytes"] == 0

    @pytest.mark.anyio
    async def test_streams_user_when_requested(self):
        """User is streamed when UsersV1 is requested."""
        user_updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(user_updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.UsersV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AlbumV1"
        assert events[0]["data"]["name"] == "Test Album"
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_owner_album_user_for_albums_v2(self):
        """The v3 client (AlbumsV2 + AlbumUsersV1) gets an owner AlbumUserV1 link.

        SyncAlbumV2 dropped ownerId, so the mobile album-list query inner-joins
        on an owner-role album-user row. Without it, albums sync into the DB but
        never display. The adapter derives that owner link from the same album
        events, streamed after the album itself (FK parent ordering).
        """
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

        request = SyncStreamDto(
            types=[SyncRequestType.AlbumsV2, SyncRequestType.AlbumUsersV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        types = [e["type"] for e in events]
        # Album parent must precede its owner album-user link (FK ordering).
        assert types == ["AlbumV2", "AlbumUserV1", "SyncCompleteV1"]

        album_user = events[1]["data"]
        assert album_user["albumId"] == str(TEST_UUID)
        assert album_user["userId"] == str(TEST_UUID)  # single-user: owner == user
        assert album_user["role"] == "owner"

    @pytest.mark.anyio
    async def test_album_users_v1_skips_album_delete_events(self):
        """The AlbumUserV1 pass must not re-emit album deletes.

        album_deleted is owned by the AlbumsV2 pass (→ AlbumDeleteV1); the
        client's album-user FK cascades on album deletion, so the derived
        album-user pass skips deletes to avoid a duplicate AlbumDeleteV1.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        album_id = uuid_to_gumnut_album_id(TEST_UUID)
        delete_event = create_mock_event(
            entity_type="album",
            entity_id=album_id,
            event_type="album_deleted",
            created_at=updated_at,
            cursor="cursor_album_del_1",
        )
        mock_client.events.get.return_value = create_mock_events_response(
            [delete_event]
        )

        request = SyncStreamDto(
            types=[SyncRequestType.AlbumsV2, SyncRequestType.AlbumUsersV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        types = [e["type"] for e in events]
        # Exactly one AlbumDeleteV1 (from the album pass), not two.
        assert types.count("AlbumDeleteV1") == 1
        assert "AlbumUserDeleteV1" not in types
        assert types[-1] == "SyncCompleteV1"

    def test_album_user_converter_maps_album_and_owner_distinctly(self):
        """The converter maps albumId and userId to distinct sources.

        The stream-level test above uses the single-user TEST_UUID for both the
        album id and the owner id, so it can't tell albumId apart from userId —
        a converter that swapped the two fields would still pass it. This pins
        them to distinct sources (album.id vs. owner_id) so a swap is caught.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        album = create_mock_album_data(updated_at)  # album.id derives from TEST_UUID
        owner_id = UUID("11111111-1111-1111-1111-111111111111")  # distinct from album

        album_user = gumnut_album_to_sync_album_user_v1(album, owner_id)

        assert album_user.albumId == TEST_UUID  # from album.id, not owner_id
        assert album_user.userId == owner_id  # from owner_id, not album.id
        assert album_user.role == AlbumUserRole.owner

    @pytest.mark.anyio
    async def test_streams_metadata_when_requested(self):
        """Metadata is streamed (as AssetExifV1) when AssetExifsV1 is requested."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        metadata_data = create_mock_metadata_data(updated_at)
        # For metadata, we need an asset with metadata attached
        asset_with_metadata = create_mock_asset_data(updated_at)
        asset_with_metadata.id = metadata_data.asset_id
        asset_with_metadata.metadata = metadata_data

        mock_event = create_mock_event(
            entity_type="metadata",
            entity_id=metadata_data.asset_id,
            event_type="metadata_updated",
            created_at=updated_at,
            cursor="cursor_metadata_1",
        )
        mock_client.events.get.return_value = create_mock_events_response([mock_event])
        mock_client.assets.list.return_value = create_mock_entity_page(
            [asset_with_metadata]
        )

        request = SyncStreamDto(types=[SyncRequestType.AssetExifsV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetExifV1"
        assert events[0]["data"]["city"] == "San Francisco"
        assert events[0]["data"]["exifImageWidth"] == 1920
        assert events[0]["data"]["exifImageHeight"] == 1080
        assert events[0]["data"]["fileSizeInByte"] == 1059218
        assert events[0]["data"]["description"] == ""
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetFaceV1"
        assert "boundingBoxX1" in events[0]["data"]
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_streams_user_metadata_preferences_with_min_faces(self):
        """UserMetadataV1 emits a synthesized preferences row with minimumFaces=1
        so people with 1-2 faces still appear in the client's People tab."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.UserMetadataV1])
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        assert len(events) == 2
        assert events[0]["type"] == "UserMetadataV1"
        data = events[0]["data"]
        assert data["key"] == "preferences"
        assert data["userId"] == str(TEST_UUID)  # owner == user, FK parent
        assert data["value"]["people"]["minimumFaces"] == 1
        # Static payload keyed off a constant cursor (emit-once).
        assert events[0]["ack"].startswith("UserMetadataV1|preferences-v1|")
        assert events[1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_user_metadata_skipped_when_checkpoint_matches(self):
        """The static preferences row is not re-emitted once the client has acked
        its constant cursor."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        request = SyncStreamDto(types=[SyncRequestType.UserMetadataV1])
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.UserMetadataV1,
            updated_at=updated_at,
            cursor="preferences-v1",
        )
        checkpoint_map = {SyncEntityType.UserMetadataV1: checkpoint}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        assert [e["type"] for e in events] == ["SyncCompleteV1"]

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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        assert len(events) == 2
        assert events[0]["type"] == "AssetFaceDeleteV1"
        assert "assetFaceId" in events[0]["data"]

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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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


class TestGenerateResetStream:
    """Tests for generate_reset_stream helper function."""

    @pytest.mark.anyio
    async def test_generates_single_reset_event(self):
        """Reset stream contains only SyncResetV1 with correct format."""
        events = []
        async for line in generate_reset_stream():
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
            owner_id=TEST_UUID,
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
            owner_id=TEST_UUID,
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

        def mock_assets_list(**kwargs: Any) -> Any:
            ids = kwargs.get("ids", [])
            matching = [all_assets_by_id[id_] for id_ in ids if id_ in all_assets_by_id]
            return create_mock_entity_page(matching)

        mock_client.assets.list.side_effect = mock_assets_list

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="asset",
            sync_entity_type=SyncEntityType.AssetV1,
            owner_id=TEST_UUID,
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
        def mock_assets_list(**kwargs: Any) -> Any:
            ids = kwargs.get("ids", [])
            matching = [assets_by_id[id_] for id_ in ids if id_ in assets_by_id]
            return create_mock_entity_page(matching)

        mock_client.assets.list.side_effect = mock_assets_list

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="asset",
            sync_entity_type=SyncEntityType.AssetV1,
            owner_id=TEST_UUID,
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=SyncStreamStats(),
            checkpoint_map={},
        ):
            results.append(item)

        assert len(results) == EVENTS_PAGE_SIZE
        # Only one API call — no second page fetch
        mock_client.events.get.assert_called_once()
