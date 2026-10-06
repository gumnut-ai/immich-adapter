"""Tests for routers/utils/stack_conversion.py."""

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID

import pytest
from gumnut import APIStatusError
from gumnut.resources.stacks import AsyncStacksResource
from gumnut.types import (
    StackAddAssetsToStackResponse,
    StackCreateStackResponse,
    StackListStacksResponse,
    StackRetrieveStackResponse,
    StackSetCoverResponse,
)
from pydantic import BaseModel

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS, GUMNUT_API_MAX_PAGE_SIZE
from routers.utils.asset_conversion import ASSET_INCLUDE
from routers.utils.concurrency import BULK_FANOUT_CONCURRENCY_LIMIT
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_stack_id,
)
from routers.utils.stack_conversion import (
    GumnutStackRow,
    TimelineStacks,
    build_asset_stack_summary,
    build_stack_response,
    fetch_stack_members,
    fetch_stack_rows,
    hydrate_stack,
    hydrate_stacks,
    resolve_effective_primary,
    resolve_timeline_stacks,
    select_timeline_cover,
)
from tests.conftest import (
    MockPaginatedListing,
    MockSyncCursorPage,
    make_gumnut_asset,
    mock_list_stacks,
    make_gumnut_stack,
    make_gumnut_stack_members,
    make_gumnut_stack_with_members,
    make_sdk_status_error,
)


def _client_returning(members):
    """A Mock client whose `assets.list` yields `members` on any call.

    `Mock(return_value=...)`, not `AsyncMock` — the SDK paginator is consumed
    with `async for`, and `AsyncMock` would wrap it in a coroutine.
    """
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage(members))
    return client


def _timeline_client(rows):
    """A Mock client for `resolve_timeline_stacks`.

    `rows` is what `list_stacks` returns for the requested IDs — omit a stack to
    model a row that vanished between the two reads. `assets.list` is a bare
    Mock so a test can assert no member read was made.
    """
    client = Mock()
    client.stacks.list_stacks = mock_list_stacks(rows)
    client.assets.list = Mock()
    return client


class TestResolveEffectivePrimary:
    """The one rule every Immich stack surface shares for picking a cover."""

    def test_pinned_cover_wins(self):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[2].id

        assert resolve_effective_primary(stack, members) is members[2]

    def test_unpinned_falls_back_to_first_live_member(self):
        stack, members = make_gumnut_stack_with_members(count=3, primary_asset_id=None)

        assert resolve_effective_primary(stack, members) is members[0]

    def test_unpinned_skips_trashed_members(self):
        """A live frame outranks a trashed one, so Immich never renders a
        trashed thumbnail for a stack that still has live members."""
        stack, members = make_gumnut_stack_with_members(
            count=3, trashed={0, 1}, primary_asset_id=None
        )

        assert resolve_effective_primary(stack, members) is members[2]

    def test_trashed_pin_is_preserved(self):
        """A pinned cover stays the cover after being trashed."""
        stack, members = make_gumnut_stack_with_members(count=3, trashed={2})
        stack.primary_asset_id = members[2].id

        assert resolve_effective_primary(stack, members) is members[2]

    def test_all_trashed_falls_back_to_first_member(self):
        """A fully-trashed stack still names a cover rather than dropping out."""
        stack, members = make_gumnut_stack_with_members(
            count=3, trashed={0, 1, 2}, primary_asset_id=None
        )

        assert resolve_effective_primary(stack, members) is members[0]

    def test_pin_absent_from_members_falls_back(self):
        """A cover that left the stack can't be named, so resolution falls
        through to the same fallbacks as an unpinned stack."""
        stack, members = make_gumnut_stack_with_members(count=2)
        stack.primary_asset_id = make_gumnut_stack_members(1, stack_id=stack.id)[0].id

        assert resolve_effective_primary(stack, members) is members[0]

    def test_member_less_stack_returns_none(self):
        stack = make_gumnut_stack(asset_count=0)

        assert resolve_effective_primary(stack, []) is None


class TestFetchStackMembers:
    @pytest.mark.anyio
    async def test_pins_the_member_read_arguments(self):
        """Pins the arguments a stack read can't be correct without — see
        `fetch_stack_members` for why each is required."""
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _client_returning(members)

        result = await fetch_stack_members(client, stack.id)

        assert result == members
        kwargs = client.assets.list.call_args.kwargs
        assert kwargs["stack_id"] == stack.id
        assert kwargs["state"] == "all"
        assert kwargs["order"] == "asc"
        assert kwargs["include"] == ASSET_INCLUDE
        assert kwargs["limit"] == GUMNUT_API_MAX_PAGE_SIZE

    @pytest.mark.anyio
    async def test_pages_past_one_full_page_of_members(self):
        """A stack larger than one page must come back whole.

        `len(result)` is the real pin — an early break or a `[:limit]` slice
        fails it. The page count only adds that consumption actually crossed a
        page boundary, so the walk is exercised rather than incidentally
        satisfied by a single oversized page.
        """
        total = GUMNUT_API_MAX_PAGE_SIZE + 50
        stack = make_gumnut_stack(asset_count=total)
        members = make_gumnut_stack_members(total, stack_id=stack.id)
        listings: list[MockPaginatedListing] = []

        def _list(**kwargs):
            listings.append(MockPaginatedListing(members, page_size=kwargs["limit"]))
            return listings[-1]

        client = Mock()
        client.assets.list = Mock(side_effect=_list)

        result = await fetch_stack_members(client, stack.id)

        assert len(result) == total
        assert listings[0].pages_fetched == 2


class TestHydrateStack:
    @pytest.mark.anyio
    async def test_converts_ids_and_resolves_cover(self):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[1].id
        client = _client_returning(members)

        hydrated = await hydrate_stack(client, stack)

        assert hydrated is not None
        assert hydrated.id == safe_uuid_from_stack_id(stack.id)
        assert hydrated.primary_asset_id == safe_uuid_from_asset_id(members[1].id)
        assert list(hydrated.members) == members

    @pytest.mark.anyio
    async def test_carries_the_gumnut_live_count(self):
        """The row's count reaches callers unchanged, so it can sit below the
        hydrated member count rather than being recomputed from it."""
        stack, members = make_gumnut_stack_with_members(
            count=3, trashed={2}, asset_count=2
        )
        client = _client_returning(members)

        hydrated = await hydrate_stack(client, stack)

        assert hydrated is not None
        assert hydrated.live_asset_count == 2
        assert len(hydrated.members) == 3

    @pytest.mark.anyio
    async def test_member_less_stack_yields_none(self):
        """Pins the member-less rule stated in `hydrate_stack`."""
        stack = make_gumnut_stack(asset_count=0)
        client = _client_returning([])

        assert await hydrate_stack(client, stack) is None

    @pytest.mark.anyio
    async def test_member_less_stack_logs_the_disagreeing_count(
        self, caplog: pytest.LogCaptureFixture
    ):
        """The dropped stack is only visible to an operator through this log.

        `stack_asset_count` is the field that makes the interesting case
        queryable — a row claiming members while the member read comes back
        empty — so pin it alongside the stack ID, using a non-zero count so the
        assertion exercises that disagreement rather than the ambiguous zero.
        """
        stack = make_gumnut_stack(asset_count=4)
        client = _client_returning([])

        with caplog.at_level(logging.WARNING, logger="routers.utils.stack_conversion"):
            assert await hydrate_stack(client, stack) is None

        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        assert getattr(records[0], "stack_id", None) == stack.id
        assert getattr(records[0], "stack_asset_count", None) == 4

    @pytest.mark.anyio
    async def test_pin_absent_from_members_logs_the_override(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Pin both IDs: the pinned one names what was lost, the effective one
        what replaced it. See `hydrate_stack` for why this case warns."""
        stack, members = make_gumnut_stack_with_members(count=2)
        departed = make_gumnut_stack_members(1, stack_id=stack.id)[0]
        stack.primary_asset_id = departed.id
        client = _client_returning(members)

        with caplog.at_level(logging.WARNING, logger="routers.utils.stack_conversion"):
            hydrated = await hydrate_stack(client, stack)

        assert hydrated is not None
        assert hydrated.primary_asset_id == safe_uuid_from_asset_id(members[0].id)
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        assert getattr(records[0], "pinned_asset_id", None) == departed.id
        assert getattr(records[0], "effective_asset_id", None) == members[0].id

    @pytest.mark.anyio
    async def test_trashed_pin_is_not_treated_as_an_override(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A trashed pin is still honoured, so it must not warn.

        It is absent from the response's `assets` but present in `members`, and
        those are different things — warning here would fire on every stack
        whose cover sits in the trash.
        """
        stack, members = make_gumnut_stack_with_members(count=3, trashed={1})
        stack.primary_asset_id = members[1].id
        client = _client_returning(members)

        with caplog.at_level(logging.WARNING, logger="routers.utils.stack_conversion"):
            hydrated = await hydrate_stack(client, stack)

        assert hydrated is not None
        assert hydrated.primary_asset_id == safe_uuid_from_asset_id(members[1].id)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    @pytest.mark.anyio
    async def test_hydrated_stack_logs_nothing(self, caplog: pytest.LogCaptureFixture):
        """The happy path must stay silent, or the warning above is just noise."""
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _client_returning(members)

        with caplog.at_level(logging.WARNING, logger="routers.utils.stack_conversion"):
            assert await hydrate_stack(client, stack) is not None

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


class TestHydrateStacks:
    @pytest.mark.anyio
    async def test_preserves_input_order_under_jittered_completion(self):
        """Results zip back to the input positionally, so a slow stack must not
        overtake a fast one."""
        stacks = [make_gumnut_stack() for _ in range(5)]
        members_by_stack = {
            stack.id: make_gumnut_stack_members(1, stack_id=stack.id)
            for stack in stacks
        }
        # Reverse-correlate delay to position, so the last input finishes first.
        delays = {stack.id: (len(stacks) - i) * 0.005 for i, stack in enumerate(stacks)}

        client = Mock()
        client.assets.list = Mock(
            side_effect=lambda **kwargs: _DelayedListing(
                members_by_stack[kwargs["stack_id"]], delays[kwargs["stack_id"]]
            )
        )

        result = await hydrate_stacks(client, stacks)

        assert [h.id for h in result if h is not None] == [
            safe_uuid_from_stack_id(stack.id) for stack in stacks
        ]

    @pytest.mark.anyio
    async def test_keeps_none_placeholders_in_position(self):
        """A member-less stack still occupies its slot, so callers can zip the
        results back against their input list."""
        stacks = [make_gumnut_stack() for _ in range(3)]
        members_by_stack = {
            stacks[0].id: make_gumnut_stack_members(1, stack_id=stacks[0].id),
            stacks[1].id: [],
            stacks[2].id: make_gumnut_stack_members(1, stack_id=stacks[2].id),
        }
        client = Mock()
        client.assets.list = Mock(
            side_effect=lambda **kwargs: MockSyncCursorPage(
                members_by_stack[kwargs["stack_id"]]
            )
        )

        result = await hydrate_stacks(client, stacks)

        assert [h is None for h in result] == [False, True, False]

    @pytest.mark.anyio
    async def test_bounds_concurrent_member_reads(self):
        """One `assets.list` walk per stack would otherwise open a read per
        stack in the page; the shared semaphore caps the in-flight count."""
        stacks = [make_gumnut_stack() for _ in range(BULK_FANOUT_CONCURRENCY_LIMIT * 3)]
        tracker = _ConcurrencyTracker()
        client = Mock()
        client.assets.list = Mock(
            side_effect=lambda **kwargs: _TrackedListing(
                make_gumnut_stack_members(1, stack_id=kwargs["stack_id"]), tracker
            )
        )

        await hydrate_stacks(client, stacks)

        assert tracker.peak > 1, "expected concurrent member reads"
        assert tracker.peak <= BULK_FANOUT_CONCURRENCY_LIMIT

    @pytest.mark.anyio
    async def test_upstream_failure_aborts_the_batch(self):
        """Pins the failure half of the asymmetry stated in `hydrate_stacks`
        (the member-less half is `test_keeps_none_placeholders_in_position`).

        The call count records that every stack's read was issued rather than
        skipped once one failed; the siblings running to completion is pinned by
        `test_concurrency.py::test_siblings_run_to_completion_after_a_failure`.
        """
        stacks = [make_gumnut_stack() for _ in range(3)]
        failing_id = stacks[1].id

        def _list(**kwargs):
            if kwargs["stack_id"] == failing_id:
                raise make_sdk_status_error(500, "upstream boom")
            return MockSyncCursorPage(
                make_gumnut_stack_members(1, stack_id=kwargs["stack_id"])
            )

        client = Mock()
        client.assets.list = Mock(side_effect=_list)

        with pytest.raises(APIStatusError):
            await hydrate_stacks(client, stacks)

        assert client.assets.list.call_count == len(stacks)

    @pytest.mark.anyio
    async def test_empty_input(self):
        assert await hydrate_stacks(Mock(), []) == []


class _DelayedListing:
    """Async iterable that sleeps before yielding, to jitter completion order."""

    def __init__(self, items, delay: float):
        self._items = items
        self._delay = delay

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        await asyncio.sleep(self._delay)
        for item in self._items:
            yield item


class _ConcurrencyTracker:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.lock = asyncio.Lock()


class _TrackedListing:
    """Async iterable that records how many member reads are in flight."""

    def __init__(self, items, tracker: _ConcurrencyTracker):
        self._items = items
        self._tracker = tracker

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        async with self._tracker.lock:
            self._tracker.active += 1
            self._tracker.peak = max(self._tracker.peak, self._tracker.active)
        try:
            await asyncio.sleep(0.01)
            for item in self._items:
                yield item
        finally:
            async with self._tracker.lock:
                self._tracker.active -= 1


class TestBuildStackResponse:
    @pytest.mark.anyio
    async def test_builds_dto_with_live_members_only(self, mock_current_user):
        """Pins the live-only rule stated in `build_stack_response`."""
        stack, members = make_gumnut_stack_with_members(count=3, trashed={2})
        stack.primary_asset_id = members[1].id
        client = _client_returning(members)

        hydrated = await hydrate_stack(client, stack)
        assert hydrated is not None
        response = build_stack_response(hydrated, mock_current_user)

        assert response.id == safe_uuid_from_stack_id(stack.id)
        assert response.primaryAssetId == safe_uuid_from_asset_id(members[1].id)
        assert {asset.id for asset in response.assets} == {
            safe_uuid_from_asset_id(member.id) for member in members[:2]
        }
        assert not any(asset.isTrashed for asset in response.assets)
        # The hydrated stack still carries the trashed member — dropping it is a
        # response-shape rule, not a change to what was fetched.
        assert len(hydrated.members) == 3

    @pytest.mark.anyio
    async def test_primary_leads_the_assets_array(self, mock_current_user):
        """Pins the `assets[0]`-is-the-cover rule from `build_stack_response`.

        The pin is deliberately *not* the API's first-returned member, so
        emitting members in fetch order fails.
        """
        stack, members = make_gumnut_stack_with_members(count=4)
        stack.primary_asset_id = members[2].id
        client = _client_returning(members)

        hydrated = await hydrate_stack(client, stack)
        assert hydrated is not None
        response = build_stack_response(hydrated, mock_current_user)

        assert response.assets[0].id == response.primaryAssetId
        # Everything else keeps capture order — the sort is stable.
        assert [asset.id for asset in response.assets[1:]] == [
            safe_uuid_from_asset_id(member.id)
            for member in (members[0], members[1], members[3])
        ]

    @pytest.mark.anyio
    async def test_trashed_pin_is_absent_from_assets(self, mock_current_user):
        """Pins the trashed-pin consequence stated in `build_stack_response`."""
        stack, members = make_gumnut_stack_with_members(count=3, trashed={1})
        stack.primary_asset_id = members[1].id
        client = _client_returning(members)

        hydrated = await hydrate_stack(client, stack)
        assert hydrated is not None
        response = build_stack_response(hydrated, mock_current_user)

        assert response.primaryAssetId == safe_uuid_from_asset_id(members[1].id)
        assert response.primaryAssetId not in {asset.id for asset in response.assets}
        assert [asset.id for asset in response.assets] == [
            safe_uuid_from_asset_id(member.id) for member in (members[0], members[2])
        ]

    @pytest.mark.anyio
    async def test_all_trashed_stack_yields_empty_assets(self, mock_current_user):
        """A fully-trashed stack still names a cover, but carries no assets."""
        stack, members = make_gumnut_stack_with_members(
            count=3, trashed={0, 1, 2}, primary_asset_id=None
        )
        client = _client_returning(members)

        hydrated = await hydrate_stack(client, stack)
        assert hydrated is not None
        response = build_stack_response(hydrated, mock_current_user)

        assert response.assets == []
        assert response.primaryAssetId == safe_uuid_from_asset_id(members[0].id)


class TestBuildAssetStackSummary:
    @pytest.mark.parametrize("asset_count", [2, 3])
    def test_maps_hydrated_stack(self, asset_count):
        hydrated = Mock(
            id=UUID("019c0477-81f0-4fef-8000-000000000001"),
            primary_asset_id=UUID("019c0477-81f0-4fef-8000-000000000002"),
            live_asset_count=asset_count,
        )

        summary = build_asset_stack_summary(hydrated)

        assert summary is not None
        assert summary.id == hydrated.id
        assert summary.primaryAssetId == hydrated.primary_asset_id
        assert summary.assetCount == asset_count

    @pytest.mark.parametrize("hydrated", [None, Mock(live_asset_count=1)])
    def test_omits_unrepresentable_stack(self, hydrated):
        assert build_asset_stack_summary(hydrated) is None


# Every stack method the planned Immich stack routes call, with the parameters
# they depend on. Adapter call sites that splat a `dict[str, Any]` erase their
# keys, so pyright reports nothing when an SDK bump renames or drops a
# parameter — the failure surfaces only when the endpoint is exercised. These
# assertions turn that into a test failure at bump time instead.
STACK_METHOD_PARAMS = {
    "create_stack": {"asset_ids", "library_id", "primary_asset_id"},
    "add_assets_to_stack": {"stack_id", "asset_ids"},
    "list_stacks": {
        "ids",
        "library_id",
        "limit",
        "origin",
        "primary_asset_id",
        "starting_after_id",
    },
    "retrieve_stack": {"stack_id"},
    "set_cover": {"stack_id", "primary_asset_id"},
    "remove_assets": {"stack_id", "asset_ids"},
    "delete": {"stack_id"},
}


@pytest.mark.parametrize(
    "method_name, expected_params", sorted(STACK_METHOD_PARAMS.items())
)
def test_sdk_stack_method_signature(method_name: str, expected_params: set[str]):
    method = getattr(AsyncStacksResource, method_name, None)
    assert method is not None, f"SDK is missing stacks.{method_name}"

    actual_params = set(inspect.signature(method).parameters)
    missing = expected_params - actual_params
    assert not missing, f"stacks.{method_name} no longer accepts {sorted(missing)}"


# The row classes `GumnutStackRow` claims are interchangeable. Nothing else
# checks that claim at runtime: every test builds rows with a `Mock`, which
# satisfies any Protocol by answering to any attribute. The stack read routes do
# pass real rows, so pyright checks the classes they use — but only those, and
# only for the calls those routes happen to make.
#
# The annotation is the actual guard — pyright rejects the list if any of these
# classes stops satisfying the Protocol. The test below re-checks it at runtime
# so an SDK bump that only runs the suite still gets a legible failure naming
# the dropped field.
STACK_ROW_CLASSES: list[type[GumnutStackRow]] = [
    StackListStacksResponse,
    StackRetrieveStackResponse,
    StackCreateStackResponse,
    StackAddAssetsToStackResponse,
    StackSetCoverResponse,
]

# Kept in step with `GumnutStackRow`'s members by the annotation above: adding a
# field there without adding it here leaves the runtime half checking less than
# the static half, but cannot let a non-conforming class through.
STACK_ROW_FIELDS = {"id", "asset_count", "primary_asset_id"}


@pytest.mark.parametrize(
    "row_cls", STACK_ROW_CLASSES, ids=lambda cls: cls.__name__.removeprefix("Stack")
)
def test_sdk_stack_rows_satisfy_protocol(row_cls: type[BaseModel]):
    missing = STACK_ROW_FIELDS - set(row_cls.model_fields)
    assert not missing, f"{row_cls.__name__} no longer carries {sorted(missing)}"


def test_asset_list_accepts_stack_id_filter():
    """Member hydration can't work without these arguments.

    `order` is here because it decides the cover of every unpinned burst; the
    call-level assertion is a Mock and so would survive the SDK dropping it.
    """
    from gumnut.resources.assets import AsyncAssetsResource

    params = set(inspect.signature(AsyncAssetsResource.list).parameters)
    assert {"stack_id", "state", "order", "include", "limit"} <= params


def test_trashed_member_fixture_is_actually_trashed():
    """Guards the builder itself: an unset `Mock.trashed_at` is truthy, which
    would make every "live" assertion above pass for the wrong reason."""
    stack, members = make_gumnut_stack_with_members(count=2, trashed={1})

    assert members[0].trashed_at is None
    assert isinstance(members[1].trashed_at, datetime)
    assert members[1].trashed_at.tzinfo is timezone.utc
    assert all(member.stack_id == stack.id for member in members)


def test_stack_member_fixture_captures_in_ascending_order():
    """Guards the other half of the builder: the cover rules below assert which
    frame is *earliest*, which means nothing if every member shares a
    timestamp."""
    _, members = make_gumnut_stack_with_members(count=3)

    captured = [member.local_datetime for member in members]
    assert captured == sorted(captured)
    assert len(set(captured)) == 3


class TestFetchStackRows:
    @pytest.mark.anyio
    async def test_reads_rows_by_id_in_one_request(self):
        stack_ids = [make_gumnut_stack().id for _ in range(3)]
        rows = [make_gumnut_stack(stack_id=stack_id) for stack_id in stack_ids]
        client = Mock()
        client.stacks.list_stacks = Mock(return_value=MockSyncCursorPage(rows))

        result = await fetch_stack_rows(client, stack_ids)

        assert result == rows
        client.stacks.list_stacks.assert_called_once()
        kwargs = client.stacks.list_stacks.call_args.kwargs
        assert kwargs["ids"] == stack_ids
        assert kwargs["limit"] == GUMNUT_API_MAX_PAGE_SIZE
        assert kwargs["include"] == ["asset_ids"]

    @pytest.mark.anyio
    async def test_chunks_past_the_bulk_id_cap(self):
        """`ids` 422s above the cap, so a bucket referencing more distinct
        stacks than that must split across requests — and lose none of them."""
        total = GUMNUT_API_MAX_BULK_IDS + 25
        rows = [make_gumnut_stack() for _ in range(total)]
        rows_by_id = {row.id: row for row in rows}
        client = Mock()
        client.stacks.list_stacks = Mock(
            side_effect=lambda **kwargs: MockSyncCursorPage(
                [rows_by_id[stack_id] for stack_id in kwargs["ids"]]
            )
        )

        result = await fetch_stack_rows(client, list(rows_by_id))

        assert len(result) == total
        assert {row.id for row in result} == set(rows_by_id)
        chunk_sizes = [
            len(call.kwargs["ids"]) for call in client.stacks.list_stacks.call_args_list
        ]
        assert chunk_sizes == [GUMNUT_API_MAX_BULK_IDS, 25]

    @pytest.mark.anyio
    async def test_walks_every_page_of_a_chunk(self):
        """A chunk can't exceed the page ceiling while both constants are 200,
        but the walk must not depend on that — a `[:limit]` slice or an early
        break fails this."""
        total = GUMNUT_API_MAX_PAGE_SIZE + 30
        rows = [make_gumnut_stack() for _ in range(total)]
        listings: list[MockPaginatedListing] = []

        def _list(**kwargs):
            listings.append(MockPaginatedListing(rows, page_size=kwargs["limit"]))
            return listings[-1]

        client = Mock()
        client.stacks.list_stacks = Mock(side_effect=_list)

        result = await fetch_stack_rows(client, [row.id for row in rows[:5]])

        assert len(result) == total
        assert listings[0].pages_fetched == 2

    @pytest.mark.anyio
    async def test_no_ids_makes_no_request(self):
        client = Mock()
        client.stacks.list_stacks = Mock()

        assert await fetch_stack_rows(client, []) == []
        client.stacks.list_stacks.assert_not_called()


class TestSelectTimelineCover:
    """The timeline's cover, chosen from the stack's live member IDs."""

    def test_live_pin_wins(self):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[2].id

        assert select_timeline_cover(stack, stack.asset_ids) == members[2].id

    def test_unpinned_falls_back_to_earliest_frame(self):
        stack, members = make_gumnut_stack_with_members(count=3, primary_asset_id=None)

        assert select_timeline_cover(stack, stack.asset_ids) == members[0].id

    def test_trashed_pin_falls_back_instead_of_hiding_the_burst(self):
        """The deliberate divergence between the two surfaces.

        `/stacks` keeps a trashed pin — `primaryAssetId` stays non-null and the
        response still lists the live frames beside it. The timeline cannot:
        collapse drops everything that is not the cover, so a cover the bucket
        cannot show would erase the burst.
        """
        stack, members = make_gumnut_stack_with_members(
            count=3, trashed={0}, primary_asset_id=None
        )
        stack.primary_asset_id = members[0].id

        assert resolve_effective_primary(stack, members) is members[0]
        assert select_timeline_cover(stack, stack.asset_ids) == members[1].id

    def test_pin_naming_no_live_frame_falls_back(self):
        stack, members = make_gumnut_stack_with_members(count=3, primary_asset_id=None)
        stack.primary_asset_id = make_gumnut_asset().id

        assert select_timeline_cover(stack, stack.asset_ids) == members[0].id

    def test_stack_with_no_live_members_has_no_cover(self):
        assert select_timeline_cover(make_gumnut_stack(), []) is None


class TestResolveTimelineStacks:
    @pytest.mark.anyio
    async def test_loose_assets_make_no_stack_requests(self):
        client = Mock()
        client.stacks.list_stacks = Mock()

        resolved = await resolve_timeline_stacks(
            client, [make_gumnut_asset() for _ in range(3)]
        )

        assert resolved.covers == {}
        assert resolved.tuples == {}
        client.stacks.list_stacks.assert_not_called()

    @pytest.mark.anyio
    async def test_resolves_the_cover_from_the_row(self):
        stack, members = make_gumnut_stack_with_members(count=3, primary_asset_id=None)
        client = _timeline_client([stack])

        resolved = await resolve_timeline_stacks(client, members)

        assert resolved.covers == {stack.id: members[0].id}
        assert resolved.tuples == {
            stack.id: [str(safe_uuid_from_stack_id(stack.id)), "3"]
        }
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_bucket_order_does_not_decide_the_cover(self):
        """Buckets arrive newest-first by default, so a rule reading bucket
        position would pick the burst's last frame."""
        stack, members = make_gumnut_stack_with_members(count=3, primary_asset_id=None)
        client = _timeline_client([stack])

        resolved = await resolve_timeline_stacks(client, list(reversed(members)))

        assert resolved.covers == {stack.id: members[0].id}

    @pytest.mark.anyio
    async def test_partial_bucket_resolves_without_a_member_read(self):
        """A burst straddling a month boundary, or a filtered bucket, holds only
        some of a stack's frames. The cover and the badge still come from the
        row: the true cover may not be in the bucket at all, and the count is
        the stack's live size, not how many frames this bucket holds."""
        stack, members = make_gumnut_stack_with_members(count=3, primary_asset_id=None)
        client = _timeline_client([stack])

        resolved = await resolve_timeline_stacks(client, members[1:])

        assert resolved.covers == {stack.id: members[0].id}
        assert resolved.tuples[stack.id][1] == "3"
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_many_partial_stacks_cost_one_row_request(self):
        """A filtered bucket can leave every stack partial; the cost stays one
        `list_stacks` request per chunk however many there are."""
        stacks, bucket = [], []
        for _ in range(GUMNUT_API_MAX_BULK_IDS):
            stack, members = make_gumnut_stack_with_members(
                count=3, primary_asset_id=None
            )
            stacks.append(stack)
            bucket.append(members[1])
        client = _timeline_client(stacks)

        resolved = await resolve_timeline_stacks(client, bucket)

        assert len(resolved.covers) == GUMNUT_API_MAX_BULK_IDS
        client.stacks.list_stacks.assert_called_once()
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_several_stacks_keep_their_own_covers(self):
        first, first_members = make_gumnut_stack_with_members(
            count=2, primary_asset_id=None
        )
        second, second_members = make_gumnut_stack_with_members(
            count=2, primary_asset_id=None
        )
        client = _timeline_client([first, second])

        resolved = await resolve_timeline_stacks(
            client, [*first_members, *second_members]
        )

        assert resolved.covers == {
            first.id: first_members[0].id,
            second.id: second_members[0].id,
        }
        client.stacks.list_stacks.assert_called_once()

    @pytest.mark.anyio
    async def test_dangling_stack_row_stays_unresolved(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A stack dissolved between the asset read and the stack read must not
        collapse its former members away — they are real photos."""
        stack, members = make_gumnut_stack_with_members(count=3)
        client = _timeline_client([])

        with caplog.at_level(logging.WARNING):
            resolved = await resolve_timeline_stacks(client, members)

        assert resolved.covers == {}
        assert resolved.tuples == {}
        assert all(not resolved.is_collapsed_away(member) for member in members)
        assert all(resolved.tuple_for(member) is None for member in members)
        assert stack.id in caplog.text

    @pytest.mark.anyio
    async def test_trashed_pin_does_not_become_the_cover(self):
        """The row keeps a trashed pin's ID, but its member IDs are live only,
        so the earliest live frame is promoted and the badge counts live
        frames."""
        stack, members = make_gumnut_stack_with_members(
            count=3, trashed={0}, primary_asset_id=None
        )
        stack.primary_asset_id = members[0].id
        client = _timeline_client([stack])

        resolved = await resolve_timeline_stacks(client, members[1:])

        assert resolved.covers == {stack.id: members[1].id}
        assert resolved.tuples[stack.id][1] == "2"

    @pytest.mark.anyio
    async def test_stack_with_no_live_members_stays_unresolved(self):
        stack, members = make_gumnut_stack_with_members(count=2, trashed={0, 1})
        client = _timeline_client([stack])

        resolved = await resolve_timeline_stacks(client, members)

        assert resolved.covers == {}
        assert all(not resolved.is_collapsed_away(member) for member in members)

    @pytest.mark.anyio
    async def test_row_without_member_ids_stays_unresolved(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A row missing the requested member IDs cannot name a cover; guessing
        one from the bucket could collapse away the real one."""
        listed, listed_members = make_gumnut_stack_with_members(
            count=2, primary_asset_id=None
        )
        unlisted, unlisted_members = make_gumnut_stack_with_members(
            count=2, primary_asset_id=None
        )
        unlisted.asset_ids = None
        client = _timeline_client([listed, unlisted])

        with caplog.at_level(logging.WARNING):
            resolved = await resolve_timeline_stacks(
                client, [*listed_members, *unlisted_members]
            )

        assert resolved.covers == {listed.id: listed_members[0].id}
        assert unlisted.id not in resolved.tuples
        assert all(
            not resolved.is_collapsed_away(member) for member in unlisted_members
        )
        (record,) = [r for r in caplog.records if hasattr(r, "unlisted_stack_count")]
        assert getattr(record, "unlisted_stack_count") == 1
        assert getattr(record, "requested_stack_count") == 2

    @pytest.mark.anyio
    async def test_undecodable_stack_id_stays_unresolved(
        self, caplog: pytest.LogCaptureFixture
    ):
        """The guard exists so a change to the `asset_stack_` prefix contract
        degrades instead of 500-ing the timeline — the adapter is that contract's
        first production consumer. Without it this raises out of the route."""
        stack, members = make_gumnut_stack_with_members(count=2, asset_count=2)
        stack.id = "asset_stackX_notdecodable"
        for member in members:
            member.stack_id = stack.id
        client = _timeline_client([stack])

        with caplog.at_level(logging.WARNING):
            resolved = await resolve_timeline_stacks(client, members)

        assert resolved.covers == {}
        assert resolved.tuples == {}
        assert all(not resolved.is_collapsed_away(member) for member in members)
        (record,) = [r for r in caplog.records if hasattr(r, "undecodable_stack_count")]
        assert getattr(record, "undecodable_stack_count") == 1


class TestTimelineStacks:
    def test_only_non_cover_members_are_collapsed_away(self):
        stack, members = make_gumnut_stack_with_members(count=3)
        resolved = TimelineStacks(
            covers={stack.id: members[1].id}, tuples={stack.id: ["uuid", "3"]}
        )

        assert not resolved.is_collapsed_away(members[1])
        assert resolved.is_collapsed_away(members[0])
        assert resolved.is_collapsed_away(members[2])

    def test_loose_asset_is_never_collapsed_and_has_no_tuple(self):
        resolved = TimelineStacks(covers={"asset_stack_x": "asset_y"}, tuples={})
        loose = make_gumnut_asset()

        assert not resolved.is_collapsed_away(loose)
        assert resolved.tuple_for(loose) is None
