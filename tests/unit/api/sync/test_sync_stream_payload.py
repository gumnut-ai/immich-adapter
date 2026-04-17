"""Tests for sync stream payload overrides (face person_id, album cover)."""

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest

from gumnut.types.face_response import FaceResponse

from routers.api.sync.fk_integrity import SyncStreamStats
from routers.api.sync.stream import _stream_entity_type, generate_sync_stream
from routers.immich_models import SyncEntityType, SyncRequestType, SyncStreamDto
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_person_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_face_id,
    uuid_to_gumnut_person_id,
)
from services.checkpoint_store import Checkpoint
from tests.unit.api.sync.conftest import (
    TEST_UUID,
    collect_stream,
    create_mock_album_data,
    create_mock_asset_data,
    create_mock_entity_page,
    create_mock_event,
    create_mock_events_response,
    create_mock_face_data,
    create_mock_gumnut_client,
    create_mock_person_data,
    create_mock_user,
)


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
        1. Face detection ran -> face_created event (face had person_id=NULL)
        2. Face clustering ran -> assigned face to person P1 (face now has person_id=P1)
        3. Sync starts -- the face_created event is within the window
        4. Adapter fetches face's CURRENT state -> gets person_id=P1
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
        1. Face detection created face F1 (person_id=NULL) -> face_created event
        2. Face clustering created person P1, assigned F1 -> person_created event
           + face_updated event, but BOTH happened after sync_started_at
        3. Sync starts with created_at_lt = sync_started_at
        4. People stream: person_created event is AFTER sync_started_at -> not returned
        5. Face stream: face_created event is BEFORE sync_started_at -> returned
        6. Adapter fetches F1 current state -> person_id=P1
        7. Adapter nulls person_id on face_created -> no FK violation

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

        # No person events in this sync window -- the person was created after
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
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

        # The adapter verifies payload-referenced people exist in production
        # before using them; mock people.list to return the referenced person.
        payload_person_data = create_mock_person_data(updated_at)
        payload_person_data.id = payload_person_id

        mock_client.events.get.return_value = create_mock_events_response([face_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])
        mock_client.people.list.return_value = create_mock_entity_page(
            [payload_person_data]
        )

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

        # Legacy event -- no payload
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
        payload_cover_assets: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Stream a single album event and return the parsed sync event data.

        ``payload_cover_assets`` lets callers control what the adapter sees
        when it verifies that a payload-referenced album_cover_asset_id still
        exists in production. Default (None): treat the referenced asset as
        extant by mirroring the payload cover id into an asset mock so the
        verification fetch succeeds. Pass an empty list to simulate the asset
        being deleted.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)
        mock_client.events.get.return_value = create_mock_events_response([album_event])
        mock_client.albums.list.return_value = create_mock_entity_page([album_data])

        if payload_cover_assets is None:
            payload_cover_id: str | None = None
            if isinstance(album_event.payload, dict):
                payload_cover_id = album_event.payload.get("album_cover_asset_id")
            payload_cover_assets = []
            if payload_cover_id:
                cover_asset = create_mock_asset_data(updated_at)
                cover_asset.id = payload_cover_id
                payload_cover_assets = [cover_asset]
        mock_client.assets.list.return_value = create_mock_entity_page(
            payload_cover_assets
        )

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

        # Legacy event -- no payload
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
        3. Sync starts -- adapter fetches person P1 -> 404 (not in fetch results)
        4. face_updated event's payload overrides person_id to P1
        5. P1 was never streamed -> must null out person_id to avoid FK violation
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # The deleted person's ID -- will return 404 (not in people.list results)
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
        # Person fetch returns empty -- person was deleted
        mock_client.people.list.return_value = create_mock_entity_page([])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])

        request = SyncStreamDto(
            types=[SyncRequestType.PeopleV1, SyncRequestType.AssetFacesV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        face_events = [e for e in events if e["type"] == "AssetFaceV1"]
        assert len(face_events) == 1

        assert face_events[0]["data"]["personId"] is None, (
            "face_updated should null person_id when the referenced person "
            "was deleted (404 on fetch) to avoid FK constraint violation"
        )

    @pytest.mark.anyio
    async def test_face_updated_keeps_person_id_when_person_synced_prior_cycle(self):
        """face_updated payload person_id is kept when person still exists in prod.

        Scenario (incremental sync, person checkpoint exists):
        1. Person P1 was synced in a prior cycle (client has it locally)
        2. face_updated event recorded with person_id=P1 in payload
        3. Person P1 is NOT modified in this sync window (no person events)
        4. Person P1 still exists in production (verification fetch returns it)
        5. The adapter should keep person_id=P1 -- the client has it locally
           and the reference is still valid

        This ensures the Fix 5 verification doesn't break incremental syncs
        where the person exists but simply wasn't modified in this window.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # Person P1 -- exists, was synced before, not modified in this window
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

        # Verification fetch: P1 still exists in production
        payload_person_data = create_mock_person_data(updated_at)
        payload_person_data.id = payload_person_id

        mock_client.events.get.return_value = create_mock_events_response([face_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])
        mock_client.people.list.return_value = create_mock_entity_page(
            [payload_person_data]
        )

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

        # Person was synced in a prior cycle -- person_id should be preserved
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        face_events_out = [e for e in events if e["type"] == "AssetFaceV1"]
        assert len(face_events_out) == 3, (
            f"Expected 3 face events, got {len(face_events_out)}"
        )

        # Event 1: payload person_id=P1, but P1 is deleted -> should be nulled
        assert face_events_out[0]["data"]["personId"] is None, (
            "First face_updated (payload person_id=P1) should null person_id "
            "because P1 was deleted (404 on fetch)"
        )

        # Event 2: null payload -> uses current state (P2)
        assert face_events_out[1]["data"]["personId"] == str(p2_uuid), (
            "Second face_updated (null payload) should use current state person_id (P2)"
        )

        # Event 3: payload person_id=P2, P2 exists -> should keep P2
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
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        face_events_out = [e for e in events if e["type"] == "AssetFaceV1"]
        assert len(face_events_out) == 2

        for i, face_event in enumerate(face_events_out):
            assert face_event["data"]["personId"] is None, (
                f"Face {i + 1} should have person_id nulled when the referenced "
                f"person was deleted (404)"
            )

    @pytest.mark.anyio
    async def test_face_updated_nulls_person_id_when_person_deleted_across_cycles(
        self,
    ):
        """Regression: payload references to persons deleted across sync cycles must be nulled.

        The prior fix's checkpoint-skip guard covered only fresh syncs: it
        skipped the null-out when a PersonV1 checkpoint existed, assuming
        "the client must still have this person from a prior sync." That
        assumption is wrong when the person was deleted server-side between
        cycles — the client's person_deleted event already removed it locally.

        Scenario:
        - Prior cycle(s): client received person P1 (checkpoint advanced past
          P1 creation) and the subsequent person_deleted P1 (checkpoint past
          delete). Client no longer has P1 locally.
        - Current cycle: no new person events (all that's left for faces).
          face_updated event for F is in the window with payload person_id=P1
          (stale reference from when clustering assigned F to P1).
        - The adapter must verify P1 still exists in prod and, seeing it
          doesn't (404), null out the reference — even though PersonV1 has a
          checkpoint.
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        # The deleted person's ID (not in the current window, not returned
        # by people.list — matches production state where the person is gone)
        deleted_person_uuid = UUID("00000000-0000-0000-0000-000000000099")
        deleted_person_id = uuid_to_gumnut_person_id(deleted_person_uuid)

        # Face currently references a different live person (re-clustered)
        face_data = create_mock_face_data(updated_at)
        assert face_data.person_id != deleted_person_id

        # face_updated event's payload carries the stale deleted person_id
        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_updated",
            created_at=updated_at,
            cursor="cursor_face_1",
            payload={"person_id": deleted_person_id},
        )

        mock_client.events.get.return_value = create_mock_events_response([face_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])
        # Verification fetch returns empty — person is deleted in prod
        mock_client.people.list.return_value = create_mock_entity_page([])

        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        # PersonV1 checkpoint exists: client synced persons in a prior cycle
        # (and processed the delete event for this person). This is the state
        # that made Fix 4 skip the null-out and leak the stale reference.
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
        json_line, _count = results[0]
        event_data = json.loads(json_line.strip())

        assert event_data["data"]["personId"] is None, (
            "face_updated must null person_id when the payload references a "
            "person deleted in a prior cycle — PersonV1 checkpoint does not "
            "prove the client still holds the person locally"
        )
        assert deleted_person_id in stats.not_found_ids["person"], (
            "Verification step should have recorded the deleted person in "
            "not_found_ids so null_deleted_fk_references can strip the reference"
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

        # The deleted asset's ID -- will return 404
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
        # Asset was deleted -- returns empty from fetch
        mock_client.assets.list.return_value = create_mock_entity_page([])
        mock_client.albums.list.return_value = create_mock_entity_page([album_data])

        request = SyncStreamDto(
            types=[SyncRequestType.AssetsV1, SyncRequestType.AlbumsV1]
        )
        checkpoint_map: dict[SyncEntityType, Checkpoint] = {}

        events = await collect_stream(
            generate_sync_stream(mock_client, request, checkpoint_map, mock_user)
        )

        album_events_out = [e for e in events if e["type"] == "AlbumV1"]
        assert len(album_events_out) == 1

        assert album_events_out[0]["data"]["thumbnailAssetId"] is None, (
            "album_updated should null album_cover_asset_id when the referenced "
            "asset was deleted (404 on fetch)"
        )

    @pytest.mark.anyio
    async def test_album_updated_nulls_cover_when_asset_deleted_across_cycles(self):
        """Regression: payload cover references to assets deleted across cycles must be nulled.

        Same cross-cycle failure mode as faces: the client has an AssetV1
        checkpoint past the cover asset's delete event. The current cycle
        replays an older album_updated event whose payload references the
        deleted asset. The adapter must verify the asset still exists in prod
        and null the reference on 404, even though an AssetV1 checkpoint
        exists (the checkpoint does not prove the client still holds the
        asset locally — it has already processed the delete).
        """
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        deleted_asset_uuid = UUID("00000000-0000-0000-0000-000000000088")
        deleted_asset_id = uuid_to_gumnut_asset_id(deleted_asset_uuid)

        album_data = create_mock_album_data(
            updated_at, album_cover_asset_id=None, asset_count=0
        )
        album_event = create_mock_event(
            entity_type="album",
            entity_id=album_data.id,
            event_type="album_updated",
            created_at=updated_at,
            cursor="cursor_a1",
            payload={"album_cover_asset_id": deleted_asset_id},
        )

        mock_client.events.get.return_value = create_mock_events_response([album_event])
        mock_client.albums.list.return_value = create_mock_entity_page([album_data])
        # Verification fetch returns empty -- the asset was deleted in prod
        mock_client.assets.list.return_value = create_mock_entity_page([])

        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        checkpoint_map = {
            SyncEntityType.AssetV1: Checkpoint(
                entity_type=SyncEntityType.AssetV1,
                cursor="prior_cursor",
                updated_at=updated_at,
            ),
        }

        stats = SyncStreamStats()
        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="album",
            sync_entity_type=SyncEntityType.AlbumV1,
            owner_id=str(TEST_UUID),
            checkpoint=None,
            sync_started_at=sync_started_at,
            stats=stats,
            checkpoint_map=checkpoint_map,
        ):
            results.append(item)

        assert len(results) == 1
        json_line, _count = results[0]
        event_data = json.loads(json_line.strip())

        assert event_data["data"]["thumbnailAssetId"] is None, (
            "album_updated must null thumbnailAssetId when the payload cover "
            "references an asset deleted across sync cycles, even with an "
            "AssetV1 checkpoint"
        )
        assert deleted_asset_id in stats.not_found_ids["asset"]


class TestAssetFaceV2Converter:
    """Tests for the AssetFaceV2 sync converter."""

    @pytest.mark.anyio
    async def test_face_v2_adds_deleted_at_and_is_visible(self):
        """AssetFaceV2 events include deletedAt=None and isVisible=True."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        face_data = create_mock_face_data(updated_at)

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
            sync_entity_type=SyncEntityType.AssetFaceV2,
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

        assert event_data["type"] == "AssetFaceV2"
        assert event_data["data"]["deletedAt"] is None
        assert event_data["data"]["isVisible"] is True
        # face_created should null out person_id
        assert event_data["data"]["personId"] is None

    @pytest.mark.anyio
    async def test_face_v2_updated_uses_payload_person_id(self):
        """face_updated V2 events use payload person_id and include V2 fields."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_user = create_mock_user(updated_at)
        mock_client = create_mock_gumnut_client(mock_user)

        face_data = create_mock_face_data(updated_at)

        # Payload carries a different person_id than current state
        payload_person_uuid = UUID("00000000-0000-0000-0000-000000000002")
        payload_person_id = uuid_to_gumnut_person_id(payload_person_uuid)

        face_event = create_mock_event(
            entity_type="face",
            entity_id=face_data.id,
            event_type="face_updated",
            created_at=updated_at,
            cursor="cursor_face_1",
            payload={"person_id": payload_person_id},
        )

        # Verification fetch: the payload-referenced person still exists in prod
        payload_person_data = create_mock_person_data(updated_at)
        payload_person_data.id = payload_person_id

        mock_client.events.get.return_value = create_mock_events_response([face_event])
        mock_client.faces.list.return_value = create_mock_entity_page([face_data])
        mock_client.people.list.return_value = create_mock_entity_page(
            [payload_person_data]
        )

        sync_started_at = datetime(2025, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

        results = []
        async for item in _stream_entity_type(
            gumnut_client=mock_client,
            gumnut_entity_type="face",
            sync_entity_type=SyncEntityType.AssetFaceV2,
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

        assert event_data["type"] == "AssetFaceV2"
        assert event_data["data"]["personId"] == str(payload_person_uuid)
        assert event_data["data"]["deletedAt"] is None
        assert event_data["data"]["isVisible"] is True
