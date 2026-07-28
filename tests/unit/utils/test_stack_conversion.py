"""Tests for routers/utils/stack_conversion.py."""

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from unittest.mock import Mock

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

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.utils.asset_conversion import ASSET_INCLUDE
from routers.utils.concurrency import BULK_FANOUT_CONCURRENCY_LIMIT
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_stack_id,
)
from routers.utils.stack_conversion import (
    GumnutStackRow,
    build_stack_response,
    fetch_stack_members,
    hydrate_stack,
    hydrate_stacks,
    resolve_effective_primary,
)
from tests.conftest import (
    MockSyncCursorPage,
    make_gumnut_stack,
    make_gumnut_stack_members,
    make_sdk_status_error,
)


def _stack_with_members(*, count: int, trashed: set[int] | None = None, **stack_kwargs):
    """Build a (stack, members) pair whose members all point at the stack."""
    stack = make_gumnut_stack(**stack_kwargs)
    members = make_gumnut_stack_members(count, stack_id=stack.id, trashed=trashed)
    return stack, members


def _client_returning(members):
    """A Mock client whose `assets.list` yields `members` on any call.

    `Mock(return_value=...)`, not `AsyncMock` — the SDK paginator is consumed
    with `async for`, and `AsyncMock` would wrap it in a coroutine.
    """
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage(members))
    return client


class TestResolveEffectivePrimary:
    """The one rule every Immich stack surface shares for picking a cover."""

    def test_pinned_cover_wins(self):
        stack, members = _stack_with_members(count=3)
        stack.primary_asset_id = members[2].id

        assert resolve_effective_primary(stack, members) is members[2]

    def test_unpinned_falls_back_to_first_live_member(self):
        stack, members = _stack_with_members(count=3, primary_asset_id=None)

        assert resolve_effective_primary(stack, members) is members[0]

    def test_unpinned_skips_trashed_members(self):
        """A live frame outranks a trashed one, so Immich never renders a
        trashed thumbnail for a stack that still has live members."""
        stack, members = _stack_with_members(
            count=3, trashed={0, 1}, primary_asset_id=None
        )

        assert resolve_effective_primary(stack, members) is members[2]

    def test_trashed_pin_is_preserved(self):
        """A pinned cover stays the cover after being trashed."""
        stack, members = _stack_with_members(count=3, trashed={2})
        stack.primary_asset_id = members[2].id

        assert resolve_effective_primary(stack, members) is members[2]

    def test_all_trashed_falls_back_to_first_member(self):
        """A fully-trashed stack still names a cover rather than dropping out."""
        stack, members = _stack_with_members(
            count=3, trashed={0, 1, 2}, primary_asset_id=None
        )

        assert resolve_effective_primary(stack, members) is members[0]

    def test_pin_absent_from_members_falls_back(self):
        """A cover that left the stack can't be named — `primaryAssetId` has to
        be one of `assets`."""
        stack, members = _stack_with_members(count=2)
        stack.primary_asset_id = make_gumnut_stack_members(1, stack_id=stack.id)[0].id

        assert resolve_effective_primary(stack, members) is members[0]

    def test_member_less_stack_returns_none(self):
        stack = make_gumnut_stack(asset_count=0)

        assert resolve_effective_primary(stack, []) is None


class TestFetchStackMembers:
    @pytest.mark.anyio
    async def test_requests_all_states_with_full_include(self):
        """Pins the arguments a stack read can't be correct without — see
        `fetch_stack_members` for why each is required."""
        stack, members = _stack_with_members(count=2)
        client = _client_returning(members)

        result = await fetch_stack_members(client, stack.id)

        assert result == members
        kwargs = client.assets.list.call_args.kwargs
        assert kwargs["stack_id"] == stack.id
        assert kwargs["state"] == "all"
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
        listings: list[_PaginatedListing] = []

        def _list(**kwargs):
            listings.append(_PaginatedListing(members, page_size=kwargs["limit"]))
            return listings[-1]

        client = Mock()
        client.assets.list = Mock(side_effect=_list)

        result = await fetch_stack_members(client, stack.id)

        assert len(result) == total
        assert listings[0].pages_fetched == 2


class _PaginatedListing:
    """Async iterator that fakes the SDK's auto-pagination contract.

    The real cursor paginator yields page-sized batches and transparently
    fetches the next page until it is exhausted. `page_size` comes from the
    `limit` the call actually sent rather than from the test, so the page count
    tracks the code's own paging rather than a number the test chose.
    """

    def __init__(self, items, page_size: int):
        self._items = items
        self._page_size = page_size
        self.pages_fetched = 0

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for i, item in enumerate(self._items):
            if i % self._page_size == 0:
                self.pages_fetched += 1
            yield item


class TestHydrateStack:
    @pytest.mark.anyio
    async def test_converts_ids_and_resolves_cover(self):
        stack, members = _stack_with_members(count=3)
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
        stack, members = _stack_with_members(count=3, trashed={2}, asset_count=2)
        client = _client_returning(members)

        hydrated = await hydrate_stack(client, stack)

        assert hydrated is not None
        assert hydrated.live_asset_count == 2
        assert len(hydrated.members) == 3

    @pytest.mark.anyio
    async def test_member_less_stack_yields_none(self):
        """No members means no honest `primaryAssetId`, so the stack drops out
        rather than shipping a fabricated UUID the client would fail to fetch."""
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
    async def test_hydrated_stack_logs_nothing(self, caplog: pytest.LogCaptureFixture):
        """The happy path must stay silent, or the warning above is just noise."""
        stack, members = _stack_with_members(count=2)
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
        """Pins the deliberate asymmetry with the member-less case.

        A member-less stack yields `None` so one bad row can't sink the
        response, but an upstream *failure* propagates and takes the batch with
        it. A route that would rather drop the single failed stack has to catch
        per stack — changing it here would change it for every caller.

        The call count records that every stack's read was issued rather than
        skipped once one failed. It does not by itself prove the siblings run
        to completion — these mocks resolve without awaiting — so treat
        `gather_with_concurrency`'s docstring as the statement of that.
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
    async def test_builds_dto_with_converted_members(self, mock_current_user):
        stack, members = _stack_with_members(count=3, trashed={2})
        stack.primary_asset_id = members[1].id
        client = _client_returning(members)

        hydrated = await hydrate_stack(client, stack)
        assert hydrated is not None
        response = build_stack_response(hydrated, mock_current_user)

        assert response.id == safe_uuid_from_stack_id(stack.id)
        assert response.primaryAssetId == safe_uuid_from_asset_id(members[1].id)
        assert [asset.id for asset in response.assets] == [
            safe_uuid_from_asset_id(member.id) for member in members
        ]
        # Trashed members belong in the response; Immich renders them in trash.
        assert [asset.isTrashed for asset in response.assets] == [False, False, True]

    @pytest.mark.anyio
    async def test_primary_is_always_one_of_the_returned_assets(
        self, mock_current_user
    ):
        """Immich resolves the cover by looking it up inside `assets`, so a
        `primaryAssetId` outside that list renders a stack with no thumbnail."""
        stack, members = _stack_with_members(
            count=3, trashed={0, 1, 2}, primary_asset_id=None
        )
        client = _client_returning(members)

        hydrated = await hydrate_stack(client, stack)
        assert hydrated is not None
        response = build_stack_response(hydrated, mock_current_user)

        assert response.primaryAssetId in {asset.id for asset in response.assets}


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
# checks that claim: every test builds rows with a `Mock`, which satisfies any
# Protocol by answering to any attribute, and while `routers/api/stacks.py` is
# stubbed there is no production call site passing a real row.
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
    """Member hydration is entirely dependent on this filter existing."""
    from gumnut.resources.assets import AsyncAssetsResource

    params = set(inspect.signature(AsyncAssetsResource.list).parameters)
    assert {"stack_id", "state", "include"} <= params


def test_trashed_member_fixture_is_actually_trashed():
    """Guards the builder itself: an unset `Mock.trashed_at` is truthy, which
    would make every "live" assertion above pass for the wrong reason."""
    stack, members = _stack_with_members(count=2, trashed={1})

    assert members[0].trashed_at is None
    assert isinstance(members[1].trashed_at, datetime)
    assert members[1].trashed_at.tzinfo is timezone.utc
    assert all(member.stack_id == stack.id for member in members)
