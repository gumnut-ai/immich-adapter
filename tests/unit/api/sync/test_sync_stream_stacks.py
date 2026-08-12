"""Tests for stacks in the event-driven sync stream."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, call

import pytest
from gumnut import GumnutError
from gumnut.types.asset_response import AssetResponse
from gumnut.types.file_data_response import FileDataResponse

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
from routers.utils.concurrency import BULK_FANOUT_CONCURRENCY_LIMIT
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
    """Return events keyed by each sync pass's entity-type filter."""
    return AsyncMock(
        side_effect=lambda **kwargs: create_mock_events_response(
            mapping.get(kwargs.get("entity_types"), [])
        )
    )


def _assets_list(*, members_by_stack=None, assets_by_id=None):
    """Serve stack-member and by-ID asset reads from one mock."""
    members_by_stack = members_by_stack or {}
    assets_by_id = assets_by_id or {}

    def _list(**kwargs):
        if "stack_id" in kwargs:
            members = members_by_stack.get(kwargs["stack_id"], [])
            if kwargs.get("state") == "live":
                members = [member for member in members if member.trashed_at is None]
            if ids := kwargs.get("ids"):
                members = [member for member in members if member.id in ids]
            return MockSyncCursorPage(members)
        ids = kwargs.get("ids") or []
        return MockSyncCursorPage([assets_by_id[i] for i in ids if i in assets_by_id])

    return Mock(side_effect=_list)


class TestAssetStackIdConversion:
    @pytest.mark.parametrize(
        "converter", [gumnut_asset_to_sync_asset_v1, gumnut_asset_to_sync_asset_v2]
    )
    def test_maps_stack_id_to_uuid_string(self, converter):
        stack = make_gumnut_stack()
        asset = make_gumnut_asset(stack_id=stack.id)

        sync = converter(asset, TEST_UUID)

        assert sync.stackId == str(safe_uuid_from_stack_id(stack.id))

    @pytest.mark.parametrize(
        "converter", [gumnut_asset_to_sync_asset_v1, gumnut_asset_to_sync_asset_v2]
    )
    def test_loose_asset_is_null(self, converter):
        asset = make_gumnut_asset(stack_id=None)

        assert converter(asset, TEST_UUID).stackId is None

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
        stack, members = make_gumnut_stack_with_members(count=3, trashed={2})
        stack.primary_asset_id = members[2].id
        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(members_by_stack={stack.id: members})

        result, missing = await fetch_entities_map(client, "stack", [stack.id])

        fetched = result[stack.id]
        assert isinstance(fetched, FetchedStack)
        assert fetched.primary_asset_id == safe_uuid_from_asset_id(members[2].id)
        assert missing == set()
        assert client.assets.list.call_args_list == [
            call(
                stack_id=stack.id,
                ids=[members[2].id],
                state="all",
                order="asc",
                limit=1,
            )
        ]

    @pytest.mark.anyio
    async def test_missing_pin_falls_back_to_first_live_member(self):
        stack, members = make_gumnut_stack_with_members(count=2)
        missing_pin = make_gumnut_asset().id
        stack.primary_asset_id = missing_pin
        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(members_by_stack={stack.id: members})

        result, _ = await fetch_entities_map(client, "stack", [stack.id])

        fetched = result[stack.id]
        assert isinstance(fetched, FetchedStack)
        assert fetched.primary_asset_id == safe_uuid_from_asset_id(members[0].id)
        assert client.assets.list.call_args_list == [
            call(
                stack_id=stack.id,
                ids=[missing_pin],
                state="all",
                order="asc",
                limit=1,
            ),
            call(stack_id=stack.id, state="live", order="asc", limit=1),
        ]

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
        assert fetched.primary_asset_id == safe_uuid_from_asset_id(members[0].id)

    @pytest.mark.anyio
    async def test_all_trashed_stack_still_resolves_primary(self):
        stack, members = make_gumnut_stack_with_members(count=2, trashed={0, 1})
        assert stack.asset_count == 0
        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = _assets_list(members_by_stack={stack.id: members})

        result, missing = await fetch_entities_map(client, "stack", [stack.id])

        fetched = result[stack.id]
        assert isinstance(fetched, FetchedStack)
        assert fetched.primary_asset_id == safe_uuid_from_asset_id(members[0].id)
        assert missing == set()
        assert client.assets.list.call_args.kwargs["state"] == "all"
        assert "include" not in client.assets.list.call_args.kwargs
        assert client.assets.list.call_args_list == [
            call(stack_id=stack.id, state="live", order="asc", limit=1),
            call(stack_id=stack.id, state="all", order="asc", limit=1),
        ]

    @pytest.mark.anyio
    async def test_primary_resolution_stops_after_first_sufficient_member(self):
        stack, members = make_gumnut_stack_with_members(count=2)
        consumed = []

        async def member_page():
            for member in members:
                consumed.append(member.id)
                yield member

        client = Mock()
        client.stacks.list_stacks = mock_list_stacks([stack])
        client.assets.list = Mock(return_value=member_page())

        result, _ = await fetch_entities_map(client, "stack", [stack.id])

        fetched = result[stack.id]
        assert isinstance(fetched, FetchedStack)
        assert fetched.primary_asset_id == safe_uuid_from_asset_id(members[0].id)
        assert consumed == [members[0].id]
        assert client.assets.list.call_args_list == [
            call(stack_id=stack.id, state="live", order="asc", limit=1)
        ]

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
    async def test_member_failure_cancels_queued_stack_reads(self):
        stacks_and_members = [
            make_gumnut_stack_with_members(count=1)
            for _ in range(BULK_FANOUT_CONCURRENCY_LIMIT + 2)
        ]
        stacks = [stack for stack, _ in stacks_and_members]
        members_by_stack = {stack.id: members for stack, members in stacks_and_members}
        bad = stacks[0]
        started = []
        client = Mock()
        client.stacks.list_stacks = Mock(return_value=MockSyncCursorPage(stacks))

        def _list(**kwargs):
            stack_id = kwargs["stack_id"]
            started.append(stack_id)
            if stack_id == bad.id:
                raise GumnutError("member read failed")
            return MockSyncCursorPage(members_by_stack[stack_id])

        client.assets.list = Mock(side_effect=_list)

        with pytest.raises(GumnutError):
            await fetch_entities_map(client, "stack", [stack.id for stack in stacks])

        assert not set(started) & {
            stack.id for stack in stacks[BULK_FANOUT_CONCURRENCY_LIMIT:]
        }

    @pytest.mark.anyio
    @pytest.mark.parametrize("asset_count", [0, 2])
    async def test_empty_member_read_propagates(self, asset_count):
        stack = make_gumnut_stack(asset_count=asset_count)
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
    async def test_asset_uses_event_time_stack_membership(self):
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        event_stack, event_stack_members = make_gumnut_stack_with_members(count=1)
        later_stack = make_gumnut_stack()
        asset_id = make_gumnut_asset().id
        asset = AssetResponse(
            id=asset_id,
            created_at=UPDATED_AT,
            local_datetime=UPDATED_AT,
            mime_type="image/jpeg",
            original_file_name="moved.jpg",
            updated_at=UPDATED_AT,
            file_data=FileDataResponse(
                checksum="sha256",
                checksum_sha1="PaDX6+c+Lhjpm5/ciXUROL1ryaU=",
                device_asset_id="device-asset",
                device_id="device",
                file_created_at=UPDATED_AT,
                file_modified_at=UPDATED_AT,
                file_size_bytes=1,
            ),
            stack_id=later_stack.id,
        )
        client.events.get = _events_by_type(
            {
                "stack": [
                    create_mock_event(
                        "stack",
                        event_stack.id,
                        "stack_created",
                        UPDATED_AT,
                        "cur_s",
                    )
                ],
                "asset": [
                    create_mock_event(
                        "asset",
                        asset.id,
                        "asset_updated",
                        UPDATED_AT,
                        "cur_a",
                        payload={"stack_id": event_stack.id},
                    )
                ],
            }
        )
        client.stacks.list_stacks = mock_list_stacks([event_stack])
        client.assets.list = _assets_list(
            members_by_stack={event_stack.id: event_stack_members},
            assets_by_id={asset.id: asset},
        )

        request = SyncStreamDto(
            types=[SyncRequestType.StacksV1, SyncRequestType.AssetsV2]
        )
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        asset_event = next(event for event in events if event["type"] == "AssetV2")
        assert asset_event["data"]["stackId"] == str(
            safe_uuid_from_stack_id(event_stack.id)
        )

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
        assert types.index("AssetDeleteV1") < types.index("StackDeleteV1")

    @pytest.mark.anyio
    async def test_moved_between_stacks_upsert_carries_new_stack_id(self):
        # A membership move is signalled as one asset_updated; the resulting
        # single AssetV2 upsert carries the new stack's stackId.
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        new_stack = make_gumnut_stack()
        asset = make_gumnut_asset(stack_id=new_stack.id)
        client.events.get = _events_by_type(
            {
                "asset": [
                    create_mock_event(
                        "asset",
                        asset.id,
                        "asset_updated",
                        UPDATED_AT,
                        "cur_a",
                        payload={"stack_id": new_stack.id},
                    )
                ],
            }
        )
        client.assets.list = _assets_list(assets_by_id={asset.id: asset})

        request = SyncStreamDto(types=[SyncRequestType.AssetsV2])
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        asset_events = [e for e in events if e["type"] == "AssetV2"]
        assert len(asset_events) == 1
        assert asset_events[0]["data"]["stackId"] == str(
            safe_uuid_from_stack_id(new_stack.id)
        )

    @pytest.mark.anyio
    async def test_dissolve_frees_member_with_null_stack_id_before_delete(self):
        # A dissolve lands stack_deleted plus the freed member's asset_updated
        # (stack_id cleared) in one window. Hydration can still observe the
        # member pointing at the about-to-be-deleted stack, so the freed asset
        # is built with stack_id=stack.id and the asset_updated payload clears
        # it to None — proving the event-time override, not the fetched state,
        # drives the emitted value. It must reach the client with stackId=None
        # (phase 1) before StackDeleteV1 (phase 2), so it is never left pointing
        # at a stack the client has already removed.
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        stack = make_gumnut_stack()
        # A real AssetResponse (not a Mock) so the event-time stack_id override
        # in the asset pass actually runs — it is gated on isinstance(AssetResponse).
        freed = AssetResponse(
            id=make_gumnut_asset().id,
            created_at=UPDATED_AT,
            local_datetime=UPDATED_AT,
            mime_type="image/jpeg",
            original_file_name="freed.jpg",
            updated_at=UPDATED_AT,
            file_data=FileDataResponse(
                checksum="sha256",
                checksum_sha1="PaDX6+c+Lhjpm5/ciXUROL1ryaU=",
                device_asset_id="device-asset",
                device_id="device",
                file_created_at=UPDATED_AT,
                file_modified_at=UPDATED_AT,
                file_size_bytes=1,
            ),
            stack_id=stack.id,
        )
        client.events.get = _events_by_type(
            {
                "stack": [
                    create_mock_event(
                        "stack", stack.id, "stack_deleted", UPDATED_AT, "cur_sd"
                    )
                ],
                "asset": [
                    create_mock_event(
                        "asset",
                        freed.id,
                        "asset_updated",
                        UPDATED_AT,
                        "cur_a",
                        payload={"stack_id": None},
                    )
                ],
            }
        )
        client.assets.list = _assets_list(assets_by_id={freed.id: freed})

        request = SyncStreamDto(
            types=[SyncRequestType.AssetsV2, SyncRequestType.StacksV1]
        )
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        types = [e["type"] for e in events]
        asset_event = next(e for e in events if e["type"] == "AssetV2")
        assert asset_event["data"]["stackId"] is None
        assert types.index("AssetV2") < types.index("StackDeleteV1")

    @pytest.mark.anyio
    async def test_caught_up_client_gets_no_stacks_and_no_sweep(self):
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)

        request = SyncStreamDto(types=[SyncRequestType.StacksV1])
        events = await collect_stream(generate_sync_stream(client, request, {}, user))

        assert [e["type"] for e in events] == ["SyncCompleteV1"]
        client.stacks.list_stacks.assert_not_called()

    @pytest.mark.anyio
    async def test_stack_hydration_failure_truncates_before_asset_pass(self, caplog):
        user = create_mock_user(UPDATED_AT)
        client = create_mock_gumnut_client(user)
        stack = make_gumnut_stack(asset_count=0)
        asset = make_gumnut_asset(stack_id=stack.id)
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
            members_by_stack={stack.id: []}, assets_by_id={asset.id: asset}
        )

        request = SyncStreamDto(
            types=[SyncRequestType.StacksV1, SyncRequestType.AssetsV2]
        )
        with caplog.at_level("ERROR"):
            events = await collect_stream(
                generate_sync_stream(client, request, {}, user)
            )

        assert not any(e["type"] in {"StackV1", "AssetV2"} for e in events)
        assert not any(e["type"] == "SyncCompleteV1" for e in events)
        assert not any(
            call.kwargs.get("ids") for call in client.assets.list.call_args_list
        )

    @pytest.mark.anyio
    async def test_stack_vanished_between_event_and_fetch_is_skipped(self, caplog):
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
        # Stacks run first, so an unguarded decode would drop every later pass.
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
