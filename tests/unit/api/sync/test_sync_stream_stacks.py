"""Tests for stacks in the event-driven sync stream.

Stacks ride the same event-cursor path as every other entity type: a
``stack_created`` / ``stack_updated`` event hydrates and emits ``StackV1``, a
``stack_deleted`` event emits ``StackDeleteV1``, and a caught-up client that
sends no new events gets nothing (no full-table sweep). The asset ``stackId``
mapping and the ``gumnut_stack_to_sync_stack_v1`` converter are exercised here
too, since both feed the stack rows the stream emits.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from gumnut import GumnutError

from routers.api.sync.converters import (
    gumnut_asset_to_sync_asset_v1,
    gumnut_asset_to_sync_asset_v2,
    gumnut_stack_to_sync_stack_v1,
)
from routers.api.sync.entity_fetch import (
    StackMemberReadInconsistent,
    fetch_entities_map,
)
from routers.api.sync.stream import generate_sync_stream
from routers.api.sync.types import FetchedStack
from routers.immich_models import SyncRequestType, SyncStreamDto
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_stack_id,
)
from tests.conftest import (
    MockSyncCursorPage,
    make_gumnut_asset,
    make_gumnut_stack,
    make_gumnut_stack_with_members,
    mock_list_stacks,
)
from tests.unit.api.sync.conftest import (
    TEST_UUID,
    collect_stream,
    create_mock_event,
    create_mock_events_response,
    create_mock_gumnut_client,
    create_mock_user,
)

UPDATED_AT = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)


def _events_by_type(mapping):
    """An ``events.get`` mock keyed on each pass's ``entity_types`` filter.

    Each entity-type pass queries ``/api/events`` for its own type, so a shared
    ``return_value`` would leak one pass's events into another. This returns the
    events registered for the requested ``entity_types`` only.
    """
    return AsyncMock(
        side_effect=lambda **kwargs: create_mock_events_response(
            mapping.get(kwargs.get("entity_types"), [])
        )
    )


def _assets_list(*, members_by_stack=None, assets_by_id=None):
    """One ``assets.list`` mock serving both stack hydration and entity fetch.

    Stack hydration reads members with ``stack_id=``; the asset pass fetches by
    ``ids=``. Dispatch on which kwarg is present so a single test can drive both.
    """
    members_by_stack = members_by_stack or {}
    assets_by_id = assets_by_id or {}

    def _list(**kwargs):
        if "stack_id" in kwargs:
            return MockSyncCursorPage(members_by_stack.get(kwargs["stack_id"], []))
        ids = kwargs.get("ids") or []
        return MockSyncCursorPage([assets_by_id[i] for i in ids if i in assets_by_id])

    return Mock(side_effect=_list)


class TestAssetStackIdConversion:
    def test_v1_maps_stack_id_to_uuid_string(self):
        stack = make_gumnut_stack()
        asset = make_gumnut_asset(stack_id=stack.id)

        sync = gumnut_asset_to_sync_asset_v1(asset, TEST_UUID)

        assert sync.stackId == str(safe_uuid_from_stack_id(stack.id))

    def test_v1_loose_asset_is_null(self):
        asset = make_gumnut_asset(stack_id=None)

        assert gumnut_asset_to_sync_asset_v1(asset, TEST_UUID).stackId is None

    def test_v2_inherits_stack_id(self):
        stack = make_gumnut_stack()
        asset = make_gumnut_asset(stack_id=stack.id)

        sync = gumnut_asset_to_sync_asset_v2(asset, TEST_UUID)

        assert sync.stackId == str(safe_uuid_from_stack_id(stack.id))

    def test_v2_loose_asset_is_null(self):
        asset = make_gumnut_asset(stack_id=None)

        assert gumnut_asset_to_sync_asset_v2(asset, TEST_UUID).stackId is None

    def test_undecodable_stack_id_degrades_to_loose_without_raising(self, caplog):
        asset = make_gumnut_asset(stack_id="not_a_valid_stack_prefix")

        with caplog.at_level("DEBUG"):
            sync = gumnut_asset_to_sync_asset_v1(asset, TEST_UUID)

        assert sync.stackId is None
        assert any("not decodable" in record.message for record in caplog.records)


class TestStackConverter:
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


class TestStackEntityFetch:
    """fetch_entities_map hydrates a stack and resolves its effective primary."""

    @pytest.mark.anyio
    async def test_resolves_pinned_primary(self):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[2].id
        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(members_by_stack={stack.id: members})

        result, missing = await fetch_entities_map(client, "stack", [stack.id])

        fetched = result[stack.id]
        assert isinstance(fetched, FetchedStack)
        assert fetched.primary_asset_id == safe_uuid_from_asset_id(members[2].id)
        assert missing == set()

    @pytest.mark.anyio
    async def test_synthesizes_primary_for_unpinned_burst(self):
        stack, members = make_gumnut_stack_with_members(count=3)
        assert stack.primary_asset_id is None
        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(members_by_stack={stack.id: members})

        result, _ = await fetch_entities_map(client, "stack", [stack.id])

        fetched = result[stack.id]
        assert isinstance(fetched, FetchedStack)
        # The earliest-captured member stands in for an unpinned burst.
        assert fetched.primary_asset_id == safe_uuid_from_asset_id(members[0].id)

    @pytest.mark.anyio
    async def test_member_less_stack_is_reported_missing(self, caplog):
        stack = make_gumnut_stack(asset_count=0)
        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(members_by_stack={stack.id: []})

        with caplog.at_level("WARNING"):
            result, missing = await fetch_entities_map(client, "stack", [stack.id])

        assert stack.id not in result
        assert stack.id in missing
        assert any("no members" in record.message for record in caplog.records)

    @pytest.mark.anyio
    async def test_two_stacks_in_one_fetch_each_keep_their_own_primary(self):
        # A page can carry several stacks, and mock_list_stacks returns rows
        # reversed vs. the requested order — so a consumer that zipped response
        # order to request order (instead of keying by row.id) would cross the
        # covers. Each stack must resolve to its own.
        stack_a, members_a = make_gumnut_stack_with_members(count=2)
        stack_a.primary_asset_id = members_a[0].id
        stack_b, members_b = make_gumnut_stack_with_members(count=3)
        stack_b.primary_asset_id = members_b[2].id
        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([stack_a, stack_b])
        client.assets.list = _assets_list(
            members_by_stack={stack_a.id: members_a, stack_b.id: members_b}
        )

        result, missing = await fetch_entities_map(
            client, "stack", [stack_a.id, stack_b.id]
        )

        assert missing == set()
        fetched_a = result[stack_a.id]
        fetched_b = result[stack_b.id]
        assert isinstance(fetched_a, FetchedStack)
        assert isinstance(fetched_b, FetchedStack)
        assert fetched_a.primary_asset_id == safe_uuid_from_asset_id(members_a[0].id)
        assert fetched_b.primary_asset_id == safe_uuid_from_asset_id(members_b[2].id)

    @pytest.mark.anyio
    async def test_transient_member_read_failure_propagates_not_skipped(self):
        # A transient member-read failure must NOT degrade to a skip: skipping
        # would advance the events cursor past the stack while its members still
        # carry stackId, permanently hiding the burst. It propagates so the sync
        # truncates and the stack retries next cycle.
        good, good_members = make_gumnut_stack_with_members(count=2)
        good.primary_asset_id = good_members[0].id
        bad, _bad_members = make_gumnut_stack_with_members(count=2)
        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([good, bad])

        def _list(**kwargs):
            if kwargs.get("stack_id") == bad.id:
                raise GumnutError("member read failed")
            return MockSyncCursorPage(
                {good.id: good_members}.get(kwargs.get("stack_id"), [])
            )

        client.assets.list = Mock(side_effect=_list)

        with pytest.raises(GumnutError):
            await fetch_entities_map(client, "stack", [good.id, bad.id])

    @pytest.mark.anyio
    async def test_empty_read_contradicting_asset_count_propagates(self):
        # The row claims members but the state="all" read comes back empty — a
        # transient contradiction, not a member-less stack (a member-less stack
        # reports asset_count 0). It must propagate to retry, not skip, or the
        # stack's members strand as a hidden burst.
        stack = make_gumnut_stack(asset_count=2)
        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(members_by_stack={stack.id: []})

        with pytest.raises(StackMemberReadInconsistent):
            await fetch_entities_map(client, "stack", [stack.id])

    @pytest.mark.anyio
    async def test_undecodable_stack_id_skips_only_that_stack(self, caplog):
        # An undecodable stack id (prefix drift) must degrade to a skip, not
        # raise out of the first sync pass and truncate everything. The decode
        # is guarded before hydration; the sibling stack still resolves.
        good, good_members = make_gumnut_stack_with_members(count=2)
        good.primary_asset_id = good_members[0].id
        bad, bad_members = make_gumnut_stack_with_members(
            count=2, stack_id="not_a_valid_stack_prefix"
        )
        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([good, bad])
        client.assets.list = _assets_list(
            members_by_stack={good.id: good_members, bad.id: bad_members}
        )

        with caplog.at_level("WARNING"):
            result, missing = await fetch_entities_map(
                client, "stack", [good.id, bad.id]
            )

        assert isinstance(result.get(good.id), FetchedStack)
        assert bad.id in missing
        assert bad.id not in result
        assert any(
            "undecodable stack id" in record.message.lower()
            for record in caplog.records
        )


class TestEventDrivenStacks:
    """Stacks flow through generate_sync_stream on the shared event-cursor path."""

    @pytest.mark.anyio
    async def test_stack_created_emits_stack_v1_from_event_cursor(self):
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        stack, members = make_gumnut_stack_with_members(count=2)
        stack.primary_asset_id = members[0].id
        client.events.get.return_value = create_mock_events_response(
            [
                create_mock_event(
                    "stack", stack.id, "stack_created", UPDATED_AT, "cur_s1"
                )
            ]
        )
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(members_by_stack={stack.id: members})

        request = SyncStreamDto(types=[SyncRequestType.StacksV1])
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        stack_events = [e for e in events if e["type"] == "StackV1"]
        assert len(stack_events) == 1
        assert stack_events[0]["data"]["id"] == str(safe_uuid_from_stack_id(stack.id))
        assert stack_events[0]["data"]["primaryAssetId"] == str(
            safe_uuid_from_asset_id(members[0].id)
        )
        # The ack is the raw event cursor, not a synthetic snapshot cursor.
        assert stack_events[0]["ack"] == "StackV1|cur_s1|"
        assert events[-1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_stack_updated_reemits_with_current_primary(self):
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[1].id
        client.events.get.return_value = create_mock_events_response(
            [
                create_mock_event(
                    "stack", stack.id, "stack_updated", UPDATED_AT, "cur_s2"
                )
            ]
        )
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(members_by_stack={stack.id: members})

        request = SyncStreamDto(types=[SyncRequestType.StacksV1])
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        stack_events = [e for e in events if e["type"] == "StackV1"]
        assert len(stack_events) == 1
        assert stack_events[0]["data"]["primaryAssetId"] == str(
            safe_uuid_from_asset_id(members[1].id)
        )

    @pytest.mark.anyio
    async def test_stack_deleted_emits_delete_without_fetching(self):
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        stack = make_gumnut_stack()
        client.events.get.return_value = create_mock_events_response(
            [
                create_mock_event(
                    "stack", stack.id, "stack_deleted", UPDATED_AT, "cur_d1"
                )
            ]
        )

        request = SyncStreamDto(types=[SyncRequestType.StacksV1])
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        delete_events = [e for e in events if e["type"] == "StackDeleteV1"]
        assert len(delete_events) == 1
        assert delete_events[0]["data"]["stackId"] == str(
            safe_uuid_from_stack_id(stack.id)
        )
        # A delete needs no entity hydration — the stack table is never read.
        client.stacks.list_stacks.assert_not_called()

    @pytest.mark.anyio
    async def test_stack_streams_before_its_stacked_asset(self):
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        stack, members = make_gumnut_stack_with_members(count=1)
        stack.primary_asset_id = members[0].id
        asset = members[0]
        client.events.get = _events_by_type(
            {
                "stack": [
                    create_mock_event(
                        "stack", stack.id, "stack_created", UPDATED_AT, "cur_s"
                    )
                ],
                "asset": [
                    create_mock_event(
                        "asset", asset.id, "asset_created", UPDATED_AT, "cur_a"
                    )
                ],
            }
        )
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(
            members_by_stack={stack.id: members}, assets_by_id={asset.id: asset}
        )

        request = SyncStreamDto(
            types=[SyncRequestType.StacksV1, SyncRequestType.AssetsV2]
        )
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        types = [e["type"] for e in events]
        assert types.index("StackV1") < types.index("AssetV2")
        stack_event = next(e for e in events if e["type"] == "StackV1")
        asset_event = next(e for e in events if e["type"] == "AssetV2")
        assert asset_event["data"]["stackId"] == stack_event["data"]["id"]
        assert asset_event["data"]["stackId"] == str(safe_uuid_from_stack_id(stack.id))
        assert events[-1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_stack_delete_streams_after_asset_delete(self):
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        stack = make_gumnut_stack()
        gone_asset_id = make_gumnut_asset().id
        client.events.get = _events_by_type(
            {
                "stack": [
                    create_mock_event(
                        "stack", stack.id, "stack_deleted", UPDATED_AT, "cur_sd"
                    )
                ],
                "asset": [
                    create_mock_event(
                        "asset", gone_asset_id, "asset_deleted", UPDATED_AT, "cur_ad"
                    )
                ],
            }
        )

        request = SyncStreamDto(
            types=[SyncRequestType.AssetsV2, SyncRequestType.StacksV1]
        )
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        types = [e["type"] for e in events]
        # Children (assets) are removed before their parent stack.
        assert types.index("AssetDeleteV1") < types.index("StackDeleteV1")

    @pytest.mark.anyio
    async def test_caught_up_client_gets_no_stacks_and_no_sweep(self):
        # No events for any type: a caught-up client. The event path emits
        # nothing and never sweeps the stack table (the snapshot path would).
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)

        request = SyncStreamDto(types=[SyncRequestType.StacksV1])
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        assert [e["type"] for e in events] == ["SyncCompleteV1"]
        client.stacks.list_stacks.assert_not_called()

    @pytest.mark.anyio
    async def test_stack_created_for_member_less_stack_is_skipped(self, caplog):
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        stack = make_gumnut_stack(asset_count=0)
        client.events.get.return_value = create_mock_events_response(
            [create_mock_event("stack", stack.id, "stack_created", UPDATED_AT, "cur_m")]
        )
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(members_by_stack={stack.id: []})

        request = SyncStreamDto(types=[SyncRequestType.StacksV1])
        with caplog.at_level("WARNING"):
            events = await collect_stream(
                generate_sync_stream(client, request, {}, user)
            )

        assert not any(e["type"] == "StackV1" for e in events)
        assert events[-1]["type"] == "SyncCompleteV1"
        assert any("no members" in record.message for record in caplog.records)

    @pytest.mark.anyio
    async def test_stack_vanished_between_event_and_fetch_is_skipped(self, caplog):
        # The stack event arrives, but the stack is gone by the time we fetch it.
        # It must be skipped, not crash the stream (the not-returned path).
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        stack = make_gumnut_stack()
        client.events.get.return_value = create_mock_events_response(
            [create_mock_event("stack", stack.id, "stack_created", UPDATED_AT, "cur_v")]
        )
        client.stacks.list_stacks = mock_list_stacks([])  # nothing comes back

        request = SyncStreamDto(types=[SyncRequestType.StacksV1])
        with caplog.at_level("WARNING"):
            events = await collect_stream(
                generate_sync_stream(client, request, {}, user)
            )

        assert not any(e["type"] == "StackV1" for e in events)
        assert events[-1]["type"] == "SyncCompleteV1"

    @pytest.mark.anyio
    async def test_partner_stacks_v1_is_silent_noop(self, caplog):
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)

        request = SyncStreamDto(types=[SyncRequestType.PartnerStacksV1])
        with caplog.at_level("INFO", logger="routers.api.sync.stream"):
            events = await collect_stream(
                generate_sync_stream(client, request, {}, user)
            )

        assert [e["type"] for e in events] == ["SyncCompleteV1"]
        assert not any(
            "unsupported" in record.message.lower() for record in caplog.records
        )
        client.stacks.list_stacks.assert_not_called()

    @pytest.mark.anyio
    async def test_undecodable_stack_deleted_event_is_skipped(self, caplog):
        # A stack_deleted with an undecodable id must skip, not truncate the
        # sync (stacks are the first pass, so an unguarded decode would take
        # every later pass and SyncCompleteV1 with it).
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        client.events.get.return_value = create_mock_events_response(
            [
                create_mock_event(
                    "stack", "not_a_valid_stack_prefix", "stack_deleted", UPDATED_AT
                )
            ]
        )

        request = SyncStreamDto(types=[SyncRequestType.StacksV1])
        with caplog.at_level("WARNING"):
            events = await collect_stream(
                generate_sync_stream(client, request, {}, user)
            )

        assert not any(e["type"] == "StackDeleteV1" for e in events)
        assert events[-1]["type"] == "SyncCompleteV1"
