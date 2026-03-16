"""Tests for sync stream event ordering (upserts before deletes)."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from routers.api.sync.fk_integrity import _GUMNUT_TYPE_TO_SYNC_TYPE
from routers.api.sync.stream import (
    _DELETE_TYPE_ORDER,
    _SYNC_TYPE_ORDER,
    generate_sync_stream,
)
from routers.immich_models import SyncEntityType, SyncRequestType, SyncStreamDto
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_face_id,
    uuid_to_gumnut_person_id,
)
from services.checkpoint_store import Checkpoint
from tests.unit.api.sync.conftest import (
    collect_stream,
    create_mock_asset_data,
    create_mock_entity_page,
    create_mock_event,
    create_mock_events_response,
    create_mock_face_data,
    create_mock_gumnut_client,
    create_mock_person_data,
    create_mock_user,
)


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

        # Reverse FK order: faces -> persons -> assets
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


class TestGumnutTypeToSyncTypeConsistency:
    """Ensure _GUMNUT_TYPE_TO_SYNC_TYPE in fk_integrity stays aligned with _SYNC_TYPE_ORDER in stream."""

    def test_fk_integrity_map_matches_stream_order(self):
        """The duplicated _GUMNUT_TYPE_TO_SYNC_TYPE must match the canonical
        _SYNC_TYPE_ORDER so FK checkpoint lookups stay correct."""
        expected = {
            gumnut_type: sync_type
            for _, gumnut_type, sync_type in _SYNC_TYPE_ORDER
        }
        assert _GUMNUT_TYPE_TO_SYNC_TYPE == expected, (
            f"_GUMNUT_TYPE_TO_SYNC_TYPE in fk_integrity.py has diverged from "
            f"_SYNC_TYPE_ORDER in stream.py.\n"
            f"  Expected: {expected}\n"
            f"  Actual:   {dict(_GUMNUT_TYPE_TO_SYNC_TYPE)}"
        )


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
