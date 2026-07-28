"""Tests for routers/utils/stack_conversion.py."""

import asyncio
import inspect
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from gumnut.resources.stacks import AsyncStacksResource

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.utils.asset_conversion import ASSET_INCLUDE
from routers.utils.concurrency import BULK_FANOUT_CONCURRENCY_LIMIT
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_stack_id,
)
from routers.utils.stack_conversion import (
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
        """An auto-detected burst has no pinned cover, so the adapter picks the
        first member in the Gumnut API's own ordering."""
        stack, members = _stack_with_members(count=3, primary_asset_id=None)

        assert resolve_effective_primary(stack, members) is members[0]

    def test_unpinned_skips_trashed_members(self):
        """Trashed members are in the response but must not become the cover
        while a live frame exists — Immich would render a trashed thumbnail."""
        stack, members = _stack_with_members(
            count=3, trashed={0, 1}, primary_asset_id=None
        )

        assert resolve_effective_primary(stack, members) is members[2]

    def test_trashed_pin_is_preserved(self):
        """The Gumnut API keeps a trashed cover's ID until permanent deletion.

        Silently re-covering the stack the moment a user trashes their chosen
        frame would override an explicit choice they can still undo.
        """
        stack, members = _stack_with_members(count=3, trashed={2})
        stack.primary_asset_id = members[2].id

        assert resolve_effective_primary(stack, members) is members[2]

    def test_all_trashed_falls_back_to_first_member(self):
        """A fully-trashed stack still has to name a cover to be representable."""
        stack, members = _stack_with_members(
            count=3, trashed={0, 1, 2}, primary_asset_id=None
        )

        assert resolve_effective_primary(stack, members) is members[0]

    def test_pin_absent_from_members_falls_back(self):
        """A cover that left the stack between the two reads can't be returned —
        Immich requires `primaryAssetId` to name one of `assets`."""
        stack, members = _stack_with_members(count=2)
        stack.primary_asset_id = make_gumnut_stack_members(1, stack_id=stack.id)[0].id

        assert resolve_effective_primary(stack, members) is members[0]

    def test_member_less_stack_returns_none(self):
        stack = make_gumnut_stack(asset_count=0)

        assert resolve_effective_primary(stack, []) is None


class TestFetchStackMembers:
    @pytest.mark.anyio
    async def test_requests_all_states_with_full_include(self):
        """Pins the three arguments a stack read can't be correct without.

        `state="all"` because a trashed pinned cover must still arrive;
        `stack_id` because that is what scopes the read to one burst; and
        `ASSET_INCLUDE` because every member is converted by
        `convert_gumnut_asset_to_immich`, which reads `metadata`, `people`, and
        the `file_data` scalars.
        """
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
        """`limit` is the per-page size, not a result cap.

        A flat in-memory listing can't tell the two apart, so this uses a mock
        that counts page boundaries: a stack larger than one page must return
        every member and visibly fetch more than one page.
        """
        stack = make_gumnut_stack(asset_count=GUMNUT_API_MAX_PAGE_SIZE + 50)
        members = make_gumnut_stack_members(
            GUMNUT_API_MAX_PAGE_SIZE + 50, stack_id=stack.id
        )
        listing = _PaginatedListing(members, page_size=GUMNUT_API_MAX_PAGE_SIZE)
        client = Mock()
        client.assets.list = Mock(return_value=listing)

        result = await fetch_stack_members(client, stack.id)

        assert len(result) == GUMNUT_API_MAX_PAGE_SIZE + 50
        assert listing.pages_fetched == 2


class _PaginatedListing:
    """Async iterator that fakes the SDK's auto-pagination contract.

    The real cursor paginator yields page-sized batches and transparently
    fetches the next page until it is exhausted. Counting page boundaries here
    is what makes "walked every page" distinguishable from "stopped at the
    first page and happened to have enough items".
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
        """The row's count excludes trashed members, so it can sit below the
        hydrated member count. Callers choose between them deliberately."""
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
