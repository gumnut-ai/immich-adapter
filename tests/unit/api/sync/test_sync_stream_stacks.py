"""Tests for Immich mobile stack-snapshot sync.

Two behaviors ship together here:

- The sync asset converters carry each asset's ``stackId`` (V1 and V2), so a
  stacked asset names the stack row it belongs to; loose assets stay null.
- ``StacksV1`` streams a *current-state snapshot* of ``StackV1`` rows on reset /
  first sync only. There is no Gumnut stack event source yet, so an
  already-synced client (checkpoint present) receives nothing — incremental
  create/update/delete support is intentionally out of scope.
"""

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from routers.api.sync.converters import (
    gumnut_asset_to_sync_asset_v1,
    gumnut_asset_to_sync_asset_v2,
    gumnut_stack_to_sync_stack_v1,
)
from routers.api.sync.stream import _stream_stacks, generate_sync_stream
from routers.immich_models import SyncEntityType, SyncRequestType, SyncStreamDto
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_stack_id,
)
from services.checkpoint_store import Checkpoint
from tests.conftest import (
    MockSyncCursorPage,
    make_gumnut_asset,
    make_gumnut_stack,
    make_gumnut_stack_with_members,
)
from tests.unit.api.sync.conftest import (
    TEST_UUID,
    collect_stream,
    create_mock_event,
    create_mock_events_response,
    create_mock_gumnut_client,
    create_mock_user,
)


def _stacks_page(*stacks):
    """A ``client.stacks.list_stacks`` mock returning ``stacks`` on any call.

    The snapshot pages with ``limit=`` and no ``ids`` filter, so — unlike the
    REST ``mock_list_stacks`` helper — this ignores kwargs and replays the full
    page in the order given, which is the order the snapshot must preserve.
    """
    return Mock(return_value=MockSyncCursorPage(list(stacks)))


def _members_by_stack(mapping):
    """A ``client.assets.list`` mock keyed by the ``stack_id`` hydrate_stack asks for."""
    return Mock(
        side_effect=lambda **kwargs: MockSyncCursorPage(
            mapping.get(kwargs.get("stack_id"), [])
        )
    )


async def _collect(gen):
    """Collect an async generator of JSON lines into parsed dicts."""
    return [json.loads(line) async for line in gen]


# --- Asset stackId conversion --------------------------------------------------


class TestAssetStackIdConversion:
    """The sync asset converters map the Gumnut stack FK to Immich's stackId."""

    def test_v1_maps_stack_id_to_uuid_string(self):
        stack = make_gumnut_stack()
        asset = make_gumnut_asset(stack_id=stack.id)

        sync = gumnut_asset_to_sync_asset_v1(asset, TEST_UUID)

        assert sync.stackId == str(safe_uuid_from_stack_id(stack.id))

    def test_v1_loose_asset_is_null(self):
        asset = make_gumnut_asset(stack_id=None)

        assert gumnut_asset_to_sync_asset_v1(asset, TEST_UUID).stackId is None

    def test_v2_inherits_stack_id(self):
        """V2 delegates to V1, so it carries the same stackId."""
        stack = make_gumnut_stack()
        asset = make_gumnut_asset(stack_id=stack.id)

        sync = gumnut_asset_to_sync_asset_v2(asset, TEST_UUID)

        assert sync.stackId == str(safe_uuid_from_stack_id(stack.id))

    def test_v2_loose_asset_is_null(self):
        asset = make_gumnut_asset(stack_id=None)

        assert gumnut_asset_to_sync_asset_v2(asset, TEST_UUID).stackId is None

    def test_undecodable_stack_id_degrades_to_loose_without_raising(self, caplog):
        """A malformed stack_id (e.g. a backend prefix change) must not abort the
        sync stream — the asset syncs as loose, with only a debug log (a prefix
        break is systemic; a per-asset warning would flood the sync)."""
        asset = make_gumnut_asset(stack_id="not_a_valid_stack_prefix")

        with caplog.at_level("DEBUG"):
            sync = gumnut_asset_to_sync_asset_v1(asset, TEST_UUID)

        assert sync.stackId is None
        assert any("not decodable" in record.message for record in caplog.records)


# --- Stack converter -----------------------------------------------------------


class TestStackConverter:
    """gumnut_stack_to_sync_stack_v1 maps a stack row + resolved primary."""

    def test_maps_all_fields(self):
        created = datetime(2025, 1, 1, tzinfo=timezone.utc)
        updated = datetime(2025, 2, 2, tzinfo=timezone.utc)
        stack = make_gumnut_stack()
        stack.created_at = created
        stack.updated_at = updated
        primary = safe_uuid_from_asset_id(make_gumnut_asset().id)

        sync = gumnut_stack_to_sync_stack_v1(stack, primary, TEST_UUID)

        assert sync.id == safe_uuid_from_stack_id(stack.id)
        assert sync.ownerId == TEST_UUID
        assert sync.primaryAssetId == primary
        assert sync.createdAt == created
        assert sync.updatedAt == updated


# --- Stack snapshot streaming (_stream_stacks) ---------------------------------


class TestStreamStacks:
    """The StacksV1 snapshot pass: reset-only, effective-primary, zero-member skip."""

    @pytest.mark.anyio
    async def test_existing_checkpoint_emits_nothing(self):
        """A present checkpoint means the client already holds the snapshot —
        emit no speculative updates and don't even page the stacks."""
        client = create_mock_gumnut_client(create_mock_user(datetime.now(timezone.utc)))
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.StackV1,
            updated_at=datetime.now(timezone.utc),
            cursor="StackV1|already-synced",
        )

        lines = await _collect(_stream_stacks(client, TEST_UUID, checkpoint))

        assert lines == []
        client.stacks.list_stacks.assert_not_called()

    @pytest.mark.anyio
    async def test_no_checkpoint_uses_pinned_primary(self):
        """The effective primary honors a pinned cover (matching REST)."""
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[2].id  # user pinned the 3rd frame
        client = Mock()
        client.stacks.list_stacks = _stacks_page(stack)
        client.assets.list = _members_by_stack({stack.id: members})

        lines = await _collect(_stream_stacks(client, TEST_UUID, None))

        assert len(lines) == 1
        assert lines[0]["type"] == "StackV1"
        assert lines[0]["data"]["primaryAssetId"] == str(
            safe_uuid_from_asset_id(members[2].id)
        )
        assert lines[0]["data"]["id"] == str(safe_uuid_from_stack_id(stack.id))

    @pytest.mark.anyio
    async def test_no_checkpoint_synthesizes_primary_for_unpinned_burst(self):
        """An unpinned burst has no server cover, so the snapshot synthesizes one:
        the first live frame (fetch_stack_members pins ascending order)."""
        stack, members = make_gumnut_stack_with_members(count=3)  # unpinned auto_burst
        assert stack.primary_asset_id is None
        client = Mock()
        client.stacks.list_stacks = _stacks_page(stack)
        client.assets.list = _members_by_stack({stack.id: members})

        lines = await _collect(_stream_stacks(client, TEST_UUID, None))

        assert lines[0]["data"]["primaryAssetId"] == str(
            safe_uuid_from_asset_id(members[0].id)
        )

    @pytest.mark.anyio
    async def test_zero_member_stack_skipped_with_warning(self, caplog):
        """A member-less stack has no honest required primary — skip it, don't
        emit an invalid row. hydrate_stack logs the warning."""
        stack = make_gumnut_stack(asset_count=0)
        client = Mock()
        client.stacks.list_stacks = _stacks_page(stack)
        client.assets.list = _members_by_stack({stack.id: []})

        with caplog.at_level("WARNING"):
            lines = await _collect(_stream_stacks(client, TEST_UUID, None))

        assert lines == []
        assert any("no members" in record.message for record in caplog.records)

    @pytest.mark.anyio
    async def test_multiple_stacks_stream_in_order_with_deterministic_acks(self):
        """Every stack streams in page order, each with a stable updated_at+id ack,
        so a reset replays deterministically."""
        stack_a, members_a = make_gumnut_stack_with_members(count=2)
        stack_a.updated_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        stack_b, members_b = make_gumnut_stack_with_members(count=2)
        stack_b.updated_at = datetime(2025, 2, 2, tzinfo=timezone.utc)

        client = Mock()
        client.stacks.list_stacks = _stacks_page(stack_a, stack_b)
        client.assets.list = _members_by_stack(
            {stack_a.id: members_a, stack_b.id: members_b}
        )

        lines = [
            (json.loads(line), line)
            async for line in _stream_stacks(client, TEST_UUID, None)
        ]

        assert [data["data"]["id"] for data, _ in lines] == [
            str(safe_uuid_from_stack_id(stack_a.id)),
            str(safe_uuid_from_stack_id(stack_b.id)),
        ]
        assert lines[0][0]["ack"] == (
            f"StackV1|{stack_a.updated_at.isoformat()}_{stack_a.id}|"
        )
        assert lines[1][0]["ack"] == (
            f"StackV1|{stack_b.updated_at.isoformat()}_{stack_b.id}|"
        )


# --- Snapshot ordering within the full stream ----------------------------------


class TestSnapshotOrdering:
    """StackV1 must precede the assets that name it, and the stream still completes."""

    @pytest.mark.anyio
    async def test_stack_streamed_before_its_stacked_asset(self):
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        user = create_mock_user(updated_at)
        client = create_mock_gumnut_client(user)

        stack, members = make_gumnut_stack_with_members(count=1)
        asset = members[0]  # the lone member, so it is also the effective primary

        client.events.get.return_value = create_mock_events_response(
            [
                create_mock_event(
                    entity_type="asset",
                    entity_id=asset.id,
                    event_type="asset_created",
                    created_at=updated_at,
                    cursor="cursor_asset_1",
                )
            ]
        )
        # assets.list serves both the sync entity fetch (ids=) and the stack
        # member fetch (stack_id=); the same stacked asset satisfies each.
        client.assets.list = Mock(
            side_effect=lambda **kwargs: MockSyncCursorPage([asset])
        )
        client.stacks.list_stacks = _stacks_page(stack)

        request = SyncStreamDto(
            types=[SyncRequestType.StacksV1, SyncRequestType.AssetsV2]
        )
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        types = [e["type"] for e in events]
        assert types.index("StackV1") < types.index("AssetV2")

        stack_event = next(e for e in events if e["type"] == "StackV1")
        asset_event = next(e for e in events if e["type"] == "AssetV2")
        # The asset points at exactly the stack row that preceded it.
        assert asset_event["data"]["stackId"] == str(safe_uuid_from_stack_id(stack.id))
        assert asset_event["data"]["stackId"] == stack_event["data"]["id"]
        assert events[-1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_existing_stack_checkpoint_streams_no_stacks(self):
        """With a StackV1 checkpoint, the full stream emits no StackV1 rows."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        user = create_mock_user(updated_at)
        client = create_mock_gumnut_client(user)
        client.stacks.list_stacks = _stacks_page(make_gumnut_stack())

        checkpoint_map = {
            SyncEntityType.StackV1: Checkpoint(
                entity_type=SyncEntityType.StackV1,
                updated_at=updated_at,
                cursor="StackV1|synced",
            )
        }
        request = SyncStreamDto(types=[SyncRequestType.StacksV1])

        events = await collect_stream(
            generate_sync_stream(client, request, checkpoint_map, user)
        )

        assert [e["type"] for e in events] == ["SyncCompleteV1"]
        client.stacks.list_stacks.assert_not_called()

    @pytest.mark.anyio
    async def test_partner_stacks_v1_is_silent_noop(self, caplog):
        """PartnerStacksV1 stays an intentional no-op: no rows, no 'unsupported'
        warning, and it never triggers the stack snapshot."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        user = create_mock_user(updated_at)
        client = create_mock_gumnut_client(user)
        client.stacks.list_stacks = _stacks_page(make_gumnut_stack())

        request = SyncStreamDto(types=[SyncRequestType.PartnerStacksV1])
        import logging

        with caplog.at_level(logging.INFO, logger="routers.api.sync.stream"):
            events = await collect_stream(
                generate_sync_stream(client, request, {}, user)
            )

        assert [e["type"] for e in events] == ["SyncCompleteV1"]
        assert not any(
            "unsupported sync types" in record.message for record in caplog.records
        )
        client.stacks.list_stacks.assert_not_called()
