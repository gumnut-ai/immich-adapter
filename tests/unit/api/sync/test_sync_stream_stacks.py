"""Tests for Immich mobile stack-snapshot sync: the asset ``stackId`` mapping and
the ``StacksV1`` upsert pass. The delivery semantics and scope boundary these
tests pin live in the "Stack Snapshot (StacksV1)" section of
``docs/architecture/sync-stream-architecture.md``.
"""

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from gumnut import GumnutError

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
    page in the order given. ``_stream_stacks`` then re-sorts by ``(updated_at,
    id)``, so passing rows out of order here exercises that ordering.
    """
    return Mock(return_value=MockSyncCursorPage(list(stacks)))


def _members_by_stack(mapping):
    """A ``client.assets.list`` mock keyed by the ``stack_id`` hydrate_stack asks for."""
    return Mock(
        side_effect=lambda **kwargs: MockSyncCursorPage(
            mapping.get(kwargs.get("stack_id"), [])
        )
    )


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


def _stack_cursor(stack):
    """The inner ack cursor `_stream_stacks` assigns a stack (no `StackV1|…|` wrapper).

    This is exactly what `_parse_ack` stores as `Checkpoint.cursor`, so a test
    can build a checkpoint that sits at a known stack's position.
    """
    return f"{stack.updated_at.isoformat()}_{stack.id}"


class TestStreamStacks:
    """The StacksV1 upsert pass: (updated_at, id) order, checkpoint filter,
    effective-primary, zero-member skip, undecodable-id degrade."""

    @pytest.mark.anyio
    async def test_checkpoint_at_or_after_all_stacks_emits_nothing(self):
        """A checkpoint at/after every current stack's cursor yields no rows —
        the client is fully caught up. The pass still re-pages (there is no
        lifecycle event stream, so every sync must scan for newer stacks)."""
        stack, members = make_gumnut_stack_with_members(count=2)
        client = Mock()
        client.stacks.list_stacks = _stacks_page(stack)
        client.assets.list = _members_by_stack({stack.id: members})
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.StackV1,
            updated_at=stack.updated_at,
            cursor=_stack_cursor(stack),  # exactly at the only stack
        )

        lines = await collect_stream(_stream_stacks(client, TEST_UUID, checkpoint))

        assert lines == []
        client.stacks.list_stacks.assert_called_once()  # re-paged, not suppressed
        # The checkpoint filter runs before hydrate_stack, so a caught-up client
        # pays zero per-stack member fetches. Locks that ordering in.
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_checkpoint_emits_only_stacks_after_its_cursor(self):
        """Incremental upsert (and, by the same mechanism, resume after a
        truncated snapshot): a checkpoint at stack_a's cursor emits only stack_b,
        so a burst created after the initial sync reaches the client instead of
        leaving its assets pointing at a stack row the client never received."""
        stack_a, members_a = make_gumnut_stack_with_members(count=2)
        stack_a.updated_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        stack_b, members_b = make_gumnut_stack_with_members(count=2)
        stack_b.updated_at = datetime(2025, 2, 2, tzinfo=timezone.utc)
        client = Mock()
        client.stacks.list_stacks = _stacks_page(stack_a, stack_b)
        client.assets.list = _members_by_stack(
            {stack_a.id: members_a, stack_b.id: members_b}
        )
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.StackV1,
            updated_at=stack_a.updated_at,
            cursor=_stack_cursor(stack_a),
        )

        lines = await collect_stream(_stream_stacks(client, TEST_UUID, checkpoint))

        assert [line["data"]["id"] for line in lines] == [
            str(safe_uuid_from_stack_id(stack_b.id))
        ]

    @pytest.mark.anyio
    async def test_undecodable_stack_id_skipped_without_aborting(self, caplog):
        """An undecodable stack id (a systemic prefix change) must not truncate
        the stream — skip that stack, still emit the decodable ones, and log one
        aggregated warning. Mirrors _immich_stack_id on the asset side."""
        good, good_members = make_gumnut_stack_with_members(count=2)
        good.updated_at = datetime(2025, 2, 2, tzinfo=timezone.utc)
        bad, bad_members = make_gumnut_stack_with_members(
            count=2, stack_id="not_a_valid_stack_prefix"
        )
        # Sorts before `good`, so the skip has to not abort the rest of the loop.
        bad.updated_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        client = Mock()
        client.stacks.list_stacks = _stacks_page(good, bad)
        client.assets.list = _members_by_stack(
            {good.id: good_members, bad.id: bad_members}
        )

        with caplog.at_level("WARNING"):
            lines = await collect_stream(_stream_stacks(client, TEST_UUID, None))

        assert [line["data"]["id"] for line in lines] == [
            str(safe_uuid_from_stack_id(good.id))
        ]
        assert any("undecodable" in record.message for record in caplog.records)

    @pytest.mark.anyio
    async def test_no_checkpoint_uses_pinned_primary(self):
        """The effective primary honors a pinned cover (matching REST)."""
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[2].id  # user pinned the 3rd frame
        client = Mock()
        client.stacks.list_stacks = _stacks_page(stack)
        client.assets.list = _members_by_stack({stack.id: members})

        lines = await collect_stream(_stream_stacks(client, TEST_UUID, None))

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

        lines = await collect_stream(_stream_stacks(client, TEST_UUID, None))

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
            lines = await collect_stream(_stream_stacks(client, TEST_UUID, None))

        assert lines == []
        assert any("no members" in record.message for record in caplog.records)

    @pytest.mark.anyio
    async def test_multiple_stacks_stream_in_order_with_deterministic_acks(self):
        """Stacks stream in (updated_at, id) order regardless of page order, each
        with a stable updated_at+id ack, so the ack cursor advances monotonically
        and a reset replays deterministically. Fed newest-first to prove the sort."""
        stack_a, members_a = make_gumnut_stack_with_members(count=2)
        stack_a.updated_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        stack_b, members_b = make_gumnut_stack_with_members(count=2)
        stack_b.updated_at = datetime(2025, 2, 2, tzinfo=timezone.utc)

        client = Mock()
        client.stacks.list_stacks = _stacks_page(stack_b, stack_a)  # out of order
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

    @pytest.mark.anyio
    async def test_checkpoint_tiebreak_on_id_at_equal_updated_at(self):
        """When two stacks share an updated_at, the id breaks the tie in both the
        sort key and the cursor comparison. A checkpoint at the lower id emits
        only the higher one — so the string cursor and the (updated_at, id) sort
        can't disagree at the same-instant boundary."""
        shared = datetime(2025, 3, 3, tzinfo=timezone.utc)
        s1, m1 = make_gumnut_stack_with_members(count=2)
        s2, m2 = make_gumnut_stack_with_members(count=2)
        s1.updated_at = s2.updated_at = shared
        # Designate low/high by actual (shortuuid-encoded) id order.
        low, high = sorted([s1, s2], key=lambda s: s.id)
        client = Mock()
        client.stacks.list_stacks = _stacks_page(high, low)  # out of order
        client.assets.list = _members_by_stack({s1.id: m1, s2.id: m2})
        checkpoint = Checkpoint(
            entity_type=SyncEntityType.StackV1,
            updated_at=shared,
            cursor=_stack_cursor(low),
        )

        lines = await collect_stream(_stream_stacks(client, TEST_UUID, checkpoint))

        assert [line["data"]["id"] for line in lines] == [
            str(safe_uuid_from_stack_id(high.id))
        ]


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
    async def test_caught_up_stack_checkpoint_streams_no_stacks(self):
        """With a StackV1 checkpoint at/after the only stack, the full stream
        emits no StackV1 rows (the client is caught up). The pass still pages —
        it must scan for stacks newer than the checkpoint."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        user = create_mock_user(updated_at)
        client = create_mock_gumnut_client(user)
        stack = make_gumnut_stack()
        client.stacks.list_stacks = _stacks_page(stack)

        checkpoint_map = {
            SyncEntityType.StackV1: Checkpoint(
                entity_type=SyncEntityType.StackV1,
                updated_at=stack.updated_at,
                cursor=_stack_cursor(stack),
            )
        }
        request = SyncStreamDto(types=[SyncRequestType.StacksV1])

        events = await collect_stream(
            generate_sync_stream(client, request, checkpoint_map, user)
        )

        assert [e["type"] for e in events] == ["SyncCompleteV1"]
        client.stacks.list_stacks.assert_called_once()

    @pytest.mark.anyio
    async def test_stack_hydration_error_does_not_truncate_sync(self, caplog):
        """A GumnutError while hydrating a stack ends the snapshot pass gracefully
        — the asset loop still runs and the stream still completes — rather than
        truncating the whole sync (the pass runs before the asset loop)."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        user = create_mock_user(updated_at)
        client = create_mock_gumnut_client(user)
        stack, members = make_gumnut_stack_with_members(count=1)
        asset = members[0]

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
        client.stacks.list_stacks = _stacks_page(stack)

        def assets_list(**kwargs):
            # The member fetch (stack_id=) fails; the asset entity fetch (ids=)
            # succeeds, so we can prove the asset loop still ran.
            if "stack_id" in kwargs:
                raise GumnutError("stack member fetch failed")
            return MockSyncCursorPage([asset])

        client.assets.list = Mock(side_effect=assets_list)

        request = SyncStreamDto(
            types=[SyncRequestType.StacksV1, SyncRequestType.AssetsV2]
        )
        with caplog.at_level("WARNING"):
            events = await collect_stream(
                generate_sync_stream(client, request, {}, user)
            )

        types = [e["type"] for e in events]
        assert "StackV1" not in types  # the only stack failed to hydrate
        assert "AssetV2" in types  # asset loop still ran this cycle
        assert events[-1]["type"] == "SyncCompleteV1"  # completed, not truncated
        assert any("cut short" in record.message for record in caplog.records)

    @pytest.mark.anyio
    async def test_partner_stacks_v1_is_silent_noop(self, caplog):
        """PartnerStacksV1 stays an intentional no-op: no rows, no 'unsupported'
        warning, and it never triggers the stack snapshot."""
        updated_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        user = create_mock_user(updated_at)
        client = create_mock_gumnut_client(user)
        client.stacks.list_stacks = _stacks_page(make_gumnut_stack())

        request = SyncStreamDto(types=[SyncRequestType.PartnerStacksV1])

        with caplog.at_level("INFO", logger="routers.api.sync.stream"):
            events = await collect_stream(
                generate_sync_stream(client, request, {}, user)
            )

        assert [e["type"] for e in events] == ["SyncCompleteV1"]
        assert not any(
            "unsupported sync types" in record.message for record in caplog.records
        )
        client.stacks.list_stacks.assert_not_called()
