"""Tests for routers/utils/stack_conversion.py."""

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

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
    uuid_to_gumnut_asset_id,
)
from routers.utils.stack_conversion import (
    MAX_TIMELINE_STACK_MEMBER_READS,
    GumnutStackRow,
    STACK_SUMMARY_COVER_READ_BUDGET,
    TimelineStacks,
    build_stack_response,
    convert_assets_with_stacks,
    fetch_live_stack_members,
    fetch_stack_cover_prefix,
    fetch_stack_members,
    fetch_stack_rows,
    hydrate_stack,
    hydrate_stacks,
    resolve_asset_stack_summaries,
    resolve_effective_primary,
    resolve_stack_cover,
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
    make_sdk_connection_error,
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


def _timeline_client(rows, *, members_by_stack: dict[str, list] | None = None):
    """A Mock client for `resolve_timeline_stacks`.

    `rows` is what `list_stacks` returns for the requested IDs — omit a stack to
    model a row that vanished between the two reads. `members_by_stack` backs the
    fallback member read; leaving it empty is how a test asserts the fast path
    made no such read.
    """
    members_by_stack = members_by_stack or {}
    client = Mock()
    client.stacks.list_stacks = mock_list_stacks(rows)
    client.assets.list = Mock(
        side_effect=lambda **kwargs: MockSyncCursorPage(
            members_by_stack.get(kwargs["stack_id"], [])
        )
    )
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
    """Async iterable that records how many member reads are in flight.

    The counter brackets the simulated round-trip *only*, releasing before the
    first item is yielded, so it measures concurrent upstream reads rather than
    generator lifetime. That distinction matters for consumers that stop early:
    `fetch_stack_cover_prefix` breaks out of its `async for` at the first live
    member, which leaves the generator suspended until GC, so a `finally` that
    wrapped the yields would never run and the count would only ever climb.
    """

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
        finally:
            async with self._tracker.lock:
                self._tracker.active -= 1
        for item in self._items:
            yield item


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

    @pytest.mark.anyio
    async def test_members_carry_no_nested_stack_block(self, mock_current_user):
        """A stack's own members ship `stack=None`, matching upstream's
        `mapStack`, which maps them without `withStack`."""
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _client_returning(members)

        hydrated = await hydrate_stack(client, stack)
        assert hydrated is not None
        response = build_stack_response(hydrated, mock_current_user)

        assert [asset.stack for asset in response.assets] == [None, None]


def _summary_client(
    rows: list[Mock], members_by_stack: dict[str, list[Mock]] | None = None
) -> Mock:
    """A Mock client answering `stacks.list_stacks` and the cover member read.

    `list_stacks` echoes back only the rows whose id was actually asked for, so
    a test can model a dangling id by leaving its row out of `rows`, and the
    chunking assertions see a realistic per-call response.
    """
    members_by_stack = members_by_stack or {}
    rows_by_id = {row.id: row for row in rows}

    client = Mock()
    client.stacks.list_stacks = Mock(
        side_effect=lambda **kwargs: MockSyncCursorPage(
            [rows_by_id[i] for i in kwargs["ids"] if i in rows_by_id]
        )
    )
    client.assets.list = Mock(
        side_effect=lambda **kwargs: MockSyncCursorPage(
            members_by_stack.get(kwargs["stack_id"], [])
        )
    )
    return client


class TestFetchStackCoverPrefix:
    @pytest.mark.anyio
    async def test_drops_only_the_heavy_include(self):
        """The cover read must keep every argument that decides *which* member
        wins, and drop only the `include` that decides how fat each row is."""
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _client_returning(members)

        await fetch_stack_cover_prefix(client, stack.id)

        kwargs = client.assets.list.call_args.kwargs
        assert kwargs["stack_id"] == stack.id
        assert kwargs["limit"] == GUMNUT_API_MAX_PAGE_SIZE
        assert "include" not in kwargs

    @pytest.mark.anyio
    async def test_stops_at_the_first_live_member(self):
        """Everything after the first live member is discarded by
        `resolve_effective_primary`, so reading it is pure waste — a burst of
        10,000 frames would otherwise cost 50 upstream pages to name a cover."""
        stack = make_gumnut_stack(primary_asset_id=None, asset_count=200)
        members = make_gumnut_stack_members(200, stack_id=stack.id, trashed={0, 1})
        listing = MockPaginatedListing(members, page_size=GUMNUT_API_MAX_PAGE_SIZE)
        client = Mock()
        client.assets.list = Mock(return_value=listing)

        result = await fetch_stack_cover_prefix(client, stack.id)

        # Two trashed frames, then the first live one — and nothing past it.
        assert result == members[:3]

    @pytest.mark.anyio
    async def test_all_trashed_stack_walks_to_exhaustion(self):
        """No live member means no early exit, which is precisely the signal
        `resolve_stack_cover` uses to tell the all-trashed case apart."""
        stack = make_gumnut_stack(primary_asset_id=None, asset_count=0)
        members = make_gumnut_stack_members(4, stack_id=stack.id, trashed={0, 1, 2, 3})
        client = _client_returning(members)

        assert await fetch_stack_cover_prefix(client, stack.id) == members


@pytest.mark.anyio
async def test_cover_walk_matches_the_member_walk():
    """The two member walks must agree on everything but the heavy include.

    `order` decides which frame an unpinned burst shows as its cover, so
    changing one walk without the other would move the cover on `/stacks` while
    leaving asset responses on the old rule — and each walk's own test pins only
    its own literals, so neither would fail.

    Compares the calls the two functions actually make rather than their source
    text, which covers every shared kwarg (including ones added later) and can't
    be fooled by a literal that also appears in a docstring.
    """
    stack, members = make_gumnut_stack_with_members(count=2)
    full, prefix = _client_returning(members), _client_returning(members)

    await fetch_stack_members(full, stack.id)
    await fetch_stack_cover_prefix(prefix, stack.id)

    assert full.assets.list.call_args.kwargs == (
        prefix.assets.list.call_args.kwargs | {"include": ASSET_INCLUDE}
    )


class TestResolveStackCover:
    @pytest.mark.anyio
    async def test_pinned_cover_costs_no_member_read(self):
        """The saving the summary path exists for: a pinned row answers from
        its own field."""
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[2].id
        client = _client_returning(members)

        cover = await resolve_stack_cover(client, stack)

        assert cover == safe_uuid_from_asset_id(members[2].id)
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_unpinned_cover_matches_hydrate_stack(self):
        """The two paths must never name different frames as one burst's cover.

        Asserted against `hydrate_stack` itself rather than a hardcoded index,
        so a future change to the effective-primary rule can't move one path
        without the other.
        """
        stack, members = make_gumnut_stack_with_members(
            count=4, trashed={0, 1}, primary_asset_id=None
        )
        client = _client_returning(members)

        cover = await resolve_stack_cover(client, stack)
        hydrated = await hydrate_stack(_client_returning(members), stack)

        assert hydrated is not None
        assert cover == hydrated.primary_asset_id
        assert cover == safe_uuid_from_asset_id(members[2].id)

    @pytest.mark.anyio
    async def test_member_less_unpinned_stack_yields_none(
        self, caplog: pytest.LogCaptureFixture
    ):
        stack = make_gumnut_stack(asset_count=4, primary_asset_id=None)
        client = _client_returning([])

        with caplog.at_level(logging.WARNING, logger="routers.utils.stack_conversion"):
            assert await resolve_stack_cover(client, stack) is None

        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        assert getattr(records[0], "stack_id", None) == stack.id

    @pytest.mark.anyio
    async def test_all_trashed_unpinned_stack_yields_none(self):
        """Decided from the members, not the row count.

        The row here *claims* live members, modelling a count that hasn't caught
        up with the trash. Trusting it would emit a summary naming a trashed
        cover, whose `GET /stacks/{id}` 404s. `resolve_effective_primary`'s
        rule 3 deliberately still names that frame for `/stacks`; the summary
        needs the stricter answer.
        """
        stack = make_gumnut_stack(asset_count=3, primary_asset_id=None)
        members = make_gumnut_stack_members(3, stack_id=stack.id, trashed={0, 1, 2})
        client = _client_returning(members)

        assert resolve_effective_primary(stack, members) is members[0]
        assert await resolve_stack_cover(client, stack) is None


class TestResolveAssetStackSummaries:
    @pytest.mark.anyio
    async def test_all_loose_assets_make_no_stack_calls(self):
        """The overwhelmingly common page. Every route calls this helper
        unconditionally, so a library with no bursts must pay nothing."""
        assets = [make_gumnut_asset() for _ in range(3)]
        client = _summary_client([])

        assert await resolve_asset_stack_summaries(client, assets) == {}

        client.stacks.list_stacks.assert_not_called()
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_shared_stack_is_read_once_for_many_assets(self):
        """Ten frames of one burst cost one row read and one cover read — the
        whole point of resolving per page instead of per asset."""
        stack, members = make_gumnut_stack_with_members(count=10, primary_asset_id=None)
        client = _summary_client([stack], {stack.id: members})

        summaries = await resolve_asset_stack_summaries(client, members)

        assert set(summaries) == {stack.id}
        assert client.stacks.list_stacks.call_count == 1
        assert client.stacks.list_stacks.call_args.kwargs["ids"] == [stack.id]
        assert client.assets.list.call_count == 1

    @pytest.mark.anyio
    async def test_pinned_stack_needs_no_cover_read(self):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[1].id
        client = _summary_client([stack], {stack.id: members})

        summaries = await resolve_asset_stack_summaries(client, members)

        assert summaries[stack.id].primaryAssetId == safe_uuid_from_asset_id(
            members[1].id
        )
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_summary_carries_id_cover_and_live_count(self):
        """All three fields come from the row, and `assetCount` is the *live*
        count — what the burst badge shows and what `/stacks` would return."""
        stack, members = make_gumnut_stack_with_members(
            count=4, trashed={3}, primary_asset_id=None
        )
        client = _summary_client([stack], {stack.id: members})

        summaries = await resolve_asset_stack_summaries(client, members)

        summary = summaries[stack.id]
        assert summary.id == safe_uuid_from_stack_id(stack.id)
        assert summary.primaryAssetId == safe_uuid_from_asset_id(members[0].id)
        assert summary.assetCount == 3

    @pytest.mark.anyio
    async def test_dangling_stack_id_is_omitted_and_warned_once(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A stack deleted between the asset read and this one appears on every
        frame it held, so the warning is per batch, not per asset."""
        stack, members = make_gumnut_stack_with_members(count=3)
        client = _summary_client([], {})

        with caplog.at_level(logging.WARNING, logger="routers.utils.stack_conversion"):
            summaries = await resolve_asset_stack_summaries(client, members)

        assert summaries == {}
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        assert getattr(records[0], "dangling_stack_ids", None) == [stack.id]

    @pytest.mark.anyio
    async def test_zero_live_member_stack_is_omitted_without_a_cover_read(self):
        """Same not-representable rule `/stacks` drops a stack for: emitting the
        summary would badge `0` and hand out an id whose detail read 404s."""
        stack, members = make_gumnut_stack_with_members(
            count=2, trashed={0, 1}, primary_asset_id=None
        )
        assert stack.asset_count == 0
        client = _summary_client([stack], {stack.id: members})

        assert await resolve_asset_stack_summaries(client, members) == {}

        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_upstream_failure_degrades_to_no_summaries(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A failed stack read must not take the page's assets down with it.

        The asset payload is the response; the stack block is decoration. And
        because the global handler forwards an upstream status verbatim, letting
        this raise would surface a stacks-lookup 404 as a 404 on the asset.
        """
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _summary_client([stack], {stack.id: members})
        client.stacks.list_stacks = Mock(
            side_effect=make_sdk_status_error(404, "Stack not found")
        )

        with caplog.at_level(logging.WARNING, logger="routers.utils.stack_conversion"):
            assert await resolve_asset_stack_summaries(client, members) == {}

        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.anyio
    async def test_cover_read_failure_costs_only_that_stack(self):
        """Cover reads are one round trip each, so a transient failure on one
        burst must not cost the badges of every other stack on the page —
        `gather_with_concurrency` would otherwise discard them all."""
        good, good_members = make_gumnut_stack_with_members(
            count=2, primary_asset_id=None
        )
        bad, bad_members = make_gumnut_stack_with_members(
            count=2, primary_asset_id=None
        )
        client = _summary_client([good, bad], {good.id: good_members})

        def _list(**kwargs):
            if kwargs["stack_id"] == bad.id:
                raise make_sdk_status_error(500, "Upstream exploded")
            return MockSyncCursorPage(good_members)

        client.assets.list = Mock(side_effect=_list)

        summaries = await resolve_asset_stack_summaries(
            client, good_members + bad_members
        )

        assert set(summaries) == {good.id}

    @pytest.mark.anyio
    async def test_single_live_member_stack_is_omitted(self):
        """Trashing one frame of a two-frame burst is ordinary user action, and
        Immich draws the badge on `asset.stack` with no count threshold — so a
        summary here would label the surviving photo a burst of "1". Upstream
        never emits that shape; it deletes a stack below two assets."""
        stack, members = make_gumnut_stack_with_members(
            count=2, trashed={1}, primary_asset_id=None
        )
        assert stack.asset_count == 1
        client = _summary_client([stack], {stack.id: members})

        assert await resolve_asset_stack_summaries(client, members) == {}

        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_two_live_members_is_the_admitting_boundary(self):
        """The other side of the rule above — two live frames is a real burst."""
        stack, members = make_gumnut_stack_with_members(count=2, primary_asset_id=None)
        assert stack.asset_count == 2
        client = _summary_client([stack], {stack.id: members})

        summaries = await resolve_asset_stack_summaries(client, members)

        assert summaries[stack.id].assetCount == 2

    @pytest.mark.anyio
    async def test_budget_is_spent_in_page_order(self):
        """Which stacks keep their badge when the budget binds should be the
        ones nearest the top of the page, not whatever order the backend
        happened to return rows in."""
        over = STACK_SUMMARY_COVER_READ_BUDGET + 2
        stacks = [make_gumnut_stack(primary_asset_id=None) for _ in range(over)]
        members_by_stack = {
            stack.id: make_gumnut_stack_members(2, stack_id=stack.id)
            for stack in stacks
        }
        assets = [make_gumnut_asset(stack_id=stack.id) for stack in stacks]

        client = _summary_client(stacks, members_by_stack)
        # Rows come back reversed, modelling a backend that doesn't echo the
        # requested id order.
        client.stacks.list_stacks = Mock(
            side_effect=lambda **kwargs: MockSyncCursorPage(
                [s for s in reversed(stacks) if s.id in set(kwargs["ids"])]
            )
        )

        summaries = await resolve_asset_stack_summaries(client, assets)

        assert set(summaries) == {
            stack.id for stack in stacks[:STACK_SUMMARY_COVER_READ_BUDGET]
        }

    @pytest.mark.anyio
    async def test_adapter_bugs_are_not_swallowed(self):
        """Only `GumnutError` degrades. A programming error here would otherwise
        become a silently missing block on every response instead of a 500."""
        stack, members = make_gumnut_stack_with_members(count=2, primary_asset_id=None)
        client = _summary_client([stack], {stack.id: members})
        client.assets.list = Mock(side_effect=TypeError("adapter bug"))

        with pytest.raises(TypeError):
            await resolve_asset_stack_summaries(client, members)

    @pytest.mark.anyio
    async def test_cover_reads_are_capped_by_the_budget(
        self, caplog: pytest.LogCaptureFixture
    ):
        """`/search/random` admits up to 1000 assets, so the unpinned-stack count
        is not bounded by any page size. Stacks past the budget ship without a
        summary rather than queueing another wave of reads."""
        over = STACK_SUMMARY_COVER_READ_BUDGET + 3
        stacks = [make_gumnut_stack(primary_asset_id=None) for _ in range(over)]
        members_by_stack = {
            stack.id: make_gumnut_stack_members(1, stack_id=stack.id)
            for stack in stacks
        }
        assets = [m for members in members_by_stack.values() for m in members]
        client = _summary_client(stacks, members_by_stack)

        with caplog.at_level(logging.WARNING, logger="routers.utils.stack_conversion"):
            summaries = await resolve_asset_stack_summaries(client, assets)

        assert len(summaries) == STACK_SUMMARY_COVER_READ_BUDGET
        assert client.assets.list.call_count == STACK_SUMMARY_COVER_READ_BUDGET
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        # Boolean under the truncation's own name, count beside it — matching
        # `stack_search_truncated` in `stacks.py` so one query form works
        # across both stack surfaces.
        assert getattr(records[0], "stack_summary_truncated", None) is True
        assert getattr(records[0], "stack_summary_truncated_stacks", None) == 3

    @pytest.mark.anyio
    async def test_exact_budget_admits_every_summary_without_truncating(
        self, caplog: pytest.LogCaptureFixture
    ):
        """At *exactly* `STACK_SUMMARY_COVER_READ_BUDGET` unpinned stacks the
        budget is spent to the last read: every summary is admitted and no
        truncation warning fires. The over-budget tests (budget+2, budget+3)
        always expect the warning, so only this boundary case rules out a
        too-eager guard that drops the last admissible row, or a warning keyed
        off input size rather than an actual drop. The `>=` vs `>` comparator
        itself is already pinned by those over-budget tests — a `>` slip admits
        one row too many there — since at exactly the budget the two comparators
        behave identically and this case can't tell them apart."""
        exactly = STACK_SUMMARY_COVER_READ_BUDGET
        stacks = [make_gumnut_stack(primary_asset_id=None) for _ in range(exactly)]
        members_by_stack = {
            stack.id: make_gumnut_stack_members(2, stack_id=stack.id)
            for stack in stacks
        }
        assets = [make_gumnut_asset(stack_id=stack.id) for stack in stacks]
        client = _summary_client(stacks, members_by_stack)

        with caplog.at_level(logging.WARNING, logger="routers.utils.stack_conversion"):
            summaries = await resolve_asset_stack_summaries(client, assets)

        assert len(summaries) == exactly
        assert client.assets.list.call_count == exactly
        assert not [
            r for r in caplog.records if getattr(r, "stack_summary_truncated", None)
        ]

    @pytest.mark.anyio
    async def test_pinned_stacks_do_not_spend_the_budget(
        self, caplog: pytest.LogCaptureFixture
    ):
        """The budget bounds *reads*, not stacks. A page of pinned stacks costs
        no reads, so truncating it would drop summaries to save nothing."""
        over = STACK_SUMMARY_COVER_READ_BUDGET + 5
        stacks = [
            make_gumnut_stack(primary_asset_id=uuid_to_gumnut_asset_id(uuid4()))
            for _ in range(over)
        ]
        assets = [make_gumnut_asset(stack_id=stack.id) for stack in stacks]
        client = _summary_client(stacks)

        with caplog.at_level(logging.WARNING, logger="routers.utils.stack_conversion"):
            summaries = await resolve_asset_stack_summaries(client, assets)

        assert len(summaries) == over
        client.assets.list.assert_not_called()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    @pytest.mark.anyio
    async def test_chunks_ids_at_the_bulk_ceiling(self):
        """`list_stacks(ids=...)` caps at `GUMNUT_API_MAX_BULK_IDS`, so a page
        touching more distinct stacks than that must split across calls."""
        total = GUMNUT_API_MAX_BULK_IDS + 5
        # Pinned, so the chunking assertion isn't diluted by cover reads.
        stacks = [
            make_gumnut_stack(primary_asset_id=uuid_to_gumnut_asset_id(uuid4()))
            for _ in range(total)
        ]
        assets = [make_gumnut_asset(stack_id=stack.id) for stack in stacks]
        client = _summary_client(stacks)

        summaries = await resolve_asset_stack_summaries(client, assets)

        assert len(summaries) == total
        sent = [call.kwargs["ids"] for call in client.stacks.list_stacks.call_args_list]
        assert [len(ids) for ids in sent] == [GUMNUT_API_MAX_BULK_IDS, 5]
        assert [i for ids in sent for i in ids] == [stack.id for stack in stacks]

    @pytest.mark.anyio
    async def test_bounds_concurrent_cover_reads(self):
        """One read per unpinned stack would otherwise open a read per burst on
        the page; the shared semaphore caps the in-flight count."""
        stacks = [
            make_gumnut_stack(primary_asset_id=None)
            for _ in range(BULK_FANOUT_CONCURRENCY_LIMIT * 3)
        ]
        members_by_stack = {
            stack.id: make_gumnut_stack_members(1, stack_id=stack.id)
            for stack in stacks
        }
        assets = [member for members in members_by_stack.values() for member in members]
        tracker = _ConcurrencyTracker()

        client = _summary_client(stacks, members_by_stack)
        client.assets.list = Mock(
            side_effect=lambda **kwargs: _TrackedListing(
                members_by_stack[kwargs["stack_id"]], tracker
            )
        )

        summaries = await resolve_asset_stack_summaries(client, assets)

        assert len(summaries) == len(stacks)
        assert tracker.peak > 1, "expected concurrent cover reads"
        assert tracker.peak <= BULK_FANOUT_CONCURRENCY_LIMIT


class TestConvertAssetsWithStacks:
    @pytest.mark.anyio
    async def test_mixed_page_keeps_order_and_other_fields(self, mock_current_user):
        """A page of loose and stacked assets converts in place: order intact,
        the stacked ones carrying a summary and the loose ones `None`."""
        stack, members = make_gumnut_stack_with_members(count=2, primary_asset_id=None)
        loose = make_gumnut_asset(original_file_name="loose.jpg")
        page = [members[0], loose, members[1]]
        client = _summary_client([stack], {stack.id: members})

        converted = await convert_assets_with_stacks(client, page, mock_current_user)

        assert [asset.id for asset in converted] == [
            safe_uuid_from_asset_id(a.id) for a in page
        ]
        assert converted[1].stack is None
        assert converted[1].originalFileName == "loose.jpg"
        stack_uuid = safe_uuid_from_stack_id(stack.id)
        assert converted[0].stack is not None and converted[0].stack.id == stack_uuid
        assert converted[2].stack is not None and converted[2].stack.id == stack_uuid
        # Every frame of one burst reports the same cover — the shared lookup is
        # what stops each asset picking its own.
        assert converted[0].stack.primaryAssetId == converted[2].stack.primaryAssetId

    @pytest.mark.anyio
    async def test_empty_page_makes_no_calls(self, mock_current_user):
        client = _summary_client([])

        assert await convert_assets_with_stacks(client, [], mock_current_user) == []

        client.stacks.list_stacks.assert_not_called()


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


class TestFetchLiveStackMembers:
    @pytest.mark.anyio
    async def test_pins_the_lean_member_read_arguments(self):
        """`state="live"` and `order="asc"` are what make the result usable as
        `select_timeline_cover`'s complete-live-set argument; no `include`,
        because the caller reads only `id`."""
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _client_returning(members)

        result = await fetch_live_stack_members(client, stack.id)

        assert result == members
        kwargs = client.assets.list.call_args.kwargs
        assert kwargs["stack_id"] == stack.id
        assert kwargs["state"] == "live"
        assert kwargs["order"] == "asc"
        assert kwargs["limit"] == GUMNUT_API_MAX_PAGE_SIZE
        assert "include" not in kwargs

    @pytest.mark.anyio
    async def test_pages_past_one_full_page_of_members(self):
        """A cover resolved from a truncated member list is the wrong frame."""
        total = GUMNUT_API_MAX_PAGE_SIZE + 30
        stack = make_gumnut_stack(asset_count=total)
        members = make_gumnut_stack_members(total, stack_id=stack.id)
        listings: list[MockPaginatedListing] = []

        def _list(**kwargs):
            listings.append(MockPaginatedListing(members, page_size=kwargs["limit"]))
            return listings[-1]

        client = Mock()
        client.assets.list = Mock(side_effect=_list)

        result = await fetch_live_stack_members(client, stack.id)

        assert len(result) == total
        assert listings[0].pages_fetched == 2


class TestSelectTimelineCover:
    """The timeline's cover — `resolve_effective_primary` over the live member
    set, returned as an ID. See `select_timeline_cover` for why the input, not
    the rule, is what the timeline changes."""

    def test_live_pin_wins(self):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[2].id

        assert select_timeline_cover(stack, members) == members[2].id

    def test_unpinned_falls_back_to_earliest_frame(self):
        stack, members = make_gumnut_stack_with_members(count=3, primary_asset_id=None)

        assert select_timeline_cover(stack, members) == members[0].id

    def test_trashed_pin_falls_back_instead_of_hiding_the_burst(self):
        """The deliberate divergence between the two surfaces, which is in the
        *input* rather than the rule.

        Both calls run the same selection, so the same input would give the same
        answer; what differs is the member set each surface resolves against.
        Handed every member, the shared rule keeps the trashed pin, which is
        what `/stacks` wants — `primaryAssetId` stays non-null and the response
        still lists the live frames beside it. Handed only the live ones, it
        promotes a frame the grid can show, which is what the timeline needs
        because collapse drops everything that is not the cover.

        A pin naming some asset that was never a member would not exercise this:
        the pin clause misses on both member sets and both fall through to the
        earliest live frame in agreement.
        """
        stack, members = make_gumnut_stack_with_members(
            count=3, trashed={0}, primary_asset_id=None
        )
        stack.primary_asset_id = members[0].id

        assert resolve_effective_primary(stack, members) is members[0]
        assert select_timeline_cover(stack, members[1:]) == members[1].id
        # Handed the trashed frame anyway, the timeline still refuses it. This
        # is the precondition being enforced rather than assumed: a cover the
        # bucket cannot show would collapse away every live frame of the burst,
        # the one degradation in this design that loses photos instead of
        # falling back. Both production callers already filter, so this input
        # is unreachable today — which is exactly why nothing else would catch
        # a third caller that forgot.
        assert select_timeline_cover(stack, members) == members[1].id

    def test_pin_naming_no_live_frame_falls_back(self):
        """A pin that left the stack entirely — no clause-1 match for either
        rule, so both promote the earliest live frame."""
        stack, members = make_gumnut_stack_with_members(count=3, primary_asset_id=None)
        stack.primary_asset_id = make_gumnut_asset().id

        assert select_timeline_cover(stack, members) == members[0].id

    def test_stack_with_no_live_members_has_no_cover(self):
        assert select_timeline_cover(make_gumnut_stack(), []) is None


class TestResolveTimelineStacks:
    @pytest.mark.anyio
    async def test_loose_assets_make_no_stack_requests(self):
        client = Mock()
        client.stacks.list_stacks = Mock()
        client.assets.list = Mock()

        resolved = await resolve_timeline_stacks(
            client, [make_gumnut_asset() for _ in range(3)]
        )

        assert resolved.covers == {}
        assert resolved.tuples == {}
        client.stacks.list_stacks.assert_not_called()
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_complete_stack_resolves_without_a_member_read(self):
        """The fast path: a bucket holding `asset_count` live members holds the
        stack's whole live set, so the cover needs no extra round-trip."""
        stack, members = make_gumnut_stack_with_members(
            count=3, asset_count=3, primary_asset_id=None
        )
        client = _timeline_client([stack])

        resolved = await resolve_timeline_stacks(client, members)

        assert resolved.covers == {stack.id: members[0].id}
        assert resolved.tuples == {
            stack.id: [str(safe_uuid_from_stack_id(stack.id)), "3"]
        }
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_bucket_order_does_not_decide_the_cover(self):
        """Buckets arrive newest-first by default, so a rule reading position
        instead of capture time would pick the burst's last frame."""
        stack, members = make_gumnut_stack_with_members(
            count=3, asset_count=3, primary_asset_id=None
        )
        client = _timeline_client([stack])

        resolved = await resolve_timeline_stacks(client, list(reversed(members)))

        assert resolved.covers == {stack.id: members[0].id}

    @pytest.mark.anyio
    async def test_partial_stack_falls_back_to_a_member_read(self):
        """A burst straddling a month boundary leaves the bucket short of the
        row's count; the true cover may not be in the bucket at all."""
        stack, members = make_gumnut_stack_with_members(
            count=3, asset_count=3, primary_asset_id=None
        )
        client = _timeline_client([stack], members_by_stack={stack.id: members})

        resolved = await resolve_timeline_stacks(client, members[1:])

        assert resolved.covers == {stack.id: members[0].id}
        kwargs = client.assets.list.call_args.kwargs
        assert kwargs["stack_id"] == stack.id
        assert kwargs["state"] == "live"
        assert kwargs["order"] == "asc"

    @pytest.mark.anyio
    async def test_several_stacks_batch_into_one_row_request(self):
        first, first_members = make_gumnut_stack_with_members(
            count=2, primary_asset_id=None
        )
        second, second_members = make_gumnut_stack_with_members(
            count=2, primary_asset_id=None
        )
        first.asset_count = 2
        second.asset_count = 2
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
    async def test_count_comes_from_the_row_not_the_bucket_members(self):
        """The badge reports the stack's live size, not how many of its frames
        this bucket happens to hold — the two differ for any straddling burst.

        Only the *bucket* count is varied here; the resolved member count still
        equals the row's, so this does not pin the row against the resolved set.
        `test_trashed_bucket_member_cannot_be_named_cover` is the fixture where
        those two diverge, and it carries that assertion.
        """
        stack, members = make_gumnut_stack_with_members(
            count=3, asset_count=3, primary_asset_id=None
        )
        client = _timeline_client([stack], members_by_stack={stack.id: members})

        resolved = await resolve_timeline_stacks(client, members[:2])

        assert resolved.tuples[stack.id][1] == "3"

    @pytest.mark.anyio
    async def test_two_partial_stacks_keep_their_own_members(self):
        """`zip(read_ids, fetched_members)` rests on the fan-out preserving input
        order. A transposition would hand one stack the other's members, pick a
        cover that is not a member, and collapse away every frame it really has."""
        first, first_members = make_gumnut_stack_with_members(
            count=3, primary_asset_id=None
        )
        second, second_members = make_gumnut_stack_with_members(
            count=3, primary_asset_id=None
        )
        first.asset_count = 3
        second.asset_count = 3
        client = _timeline_client(
            [first, second],
            members_by_stack={first.id: first_members, second.id: second_members},
        )

        resolved = await resolve_timeline_stacks(
            client, [first_members[1], second_members[1]]
        )

        assert resolved.covers == {
            first.id: first_members[0].id,
            second.id: second_members[0].id,
        }

    @pytest.mark.anyio
    async def test_mixed_naive_and_aware_capture_times_still_sort(self):
        """`local_datetime` can arrive naive or aware, and comparing the two
        raises — which would 500 a whole month. Without the UTC normalization in
        the sort key this raises instead of resolving."""
        stack, members = make_gumnut_stack_with_members(
            count=3, asset_count=3, primary_asset_id=None
        )
        members[1].local_datetime = members[1].local_datetime.replace(tzinfo=None)
        client = _timeline_client([stack])

        resolved = await resolve_timeline_stacks(client, members)

        assert resolved.covers[stack.id] == members[0].id

    @pytest.mark.anyio
    async def test_failed_member_read_leaves_only_that_stack_uncollapsed(
        self, caplog: pytest.LogCaptureFixture
    ):
        """One flaky read should cost that stack its collapse, not the batch —
        `gather_with_concurrency` would otherwise propagate and lose the month."""
        healthy, healthy_members = make_gumnut_stack_with_members(
            count=3, primary_asset_id=None
        )
        broken, broken_members = make_gumnut_stack_with_members(
            count=3, primary_asset_id=None
        )
        healthy.asset_count = 3
        broken.asset_count = 3

        def _list_assets(**kwargs):
            if kwargs["stack_id"] == broken.id:
                raise make_sdk_status_error(500, "Internal Server Error")
            return MockSyncCursorPage(healthy_members)

        client = _timeline_client([healthy, broken])
        client.assets.list = Mock(side_effect=_list_assets)

        with caplog.at_level(logging.WARNING):
            resolved = await resolve_timeline_stacks(
                client, [healthy_members[1], broken_members[1]]
            )

        assert resolved.covers == {healthy.id: healthy_members[0].id}
        assert broken.id not in resolved.tuples
        # The degradation is invisible to the user by construction, so the
        # record is the only signal an operator gets. Keyed on `extra` rather
        # than message text so a reword doesn't break it.
        (record,) = [r for r in caplog.records if hasattr(r, "failed_stack_count")]
        assert getattr(record, "failed_stack_count") == 1
        assert getattr(record, "attempted_stack_count") == 2
        # The counts say how many; these say why — see the log site for why
        # this record has to carry both.
        assert getattr(record, "failed_error_type") == "APIStatusError"
        assert getattr(record, "failed_status_code") == 500
        assert record.exc_info is not None

    @pytest.mark.anyio
    async def test_a_statusless_failure_still_names_its_cause(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Not every SDK failure carries a status.

        `APIConnectionError` and `APITimeoutError` never reach a response, so
        they have no `status_code` at all — the record reads it defensively for
        exactly this case. A transport failure is also the one an operator most
        needs distinguished from a 4xx, since it points at the network rather
        than at the request.
        """
        stack, members = make_gumnut_stack_with_members(
            count=3, asset_count=3, primary_asset_id=None
        )
        client = _timeline_client([stack])
        client.assets.list = Mock(side_effect=make_sdk_connection_error())

        with caplog.at_level(logging.WARNING):
            resolved = await resolve_timeline_stacks(client, [members[1]])

        assert resolved.covers == {}
        (record,) = [r for r in caplog.records if hasattr(r, "failed_stack_count")]
        assert getattr(record, "failed_error_type") == "APIConnectionError"
        assert getattr(record, "failed_status_code") is None

    @pytest.mark.anyio
    async def test_member_reads_are_capped_per_request(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A person-filtered bucket can leave every stack partial, so the total
        read count — not just the in-flight count — needs a bound."""
        total = MAX_TIMELINE_STACK_MEMBER_READS + 5
        stacks, bucket, members_by_stack = [], [], {}
        for _ in range(total):
            stack, members = make_gumnut_stack_with_members(
                count=3, asset_count=3, primary_asset_id=None
            )
            stacks.append(stack)
            members_by_stack[stack.id] = members
            bucket.append(members[1])
        client = _timeline_client(stacks, members_by_stack=members_by_stack)

        with caplog.at_level(logging.WARNING):
            resolved = await resolve_timeline_stacks(client, bucket)

        assert client.assets.list.call_count == MAX_TIMELINE_STACK_MEMBER_READS
        # Capped in bucket order, so the resolved set is the front of the month
        # rather than an arbitrary slice of whatever order the rows arrived in.
        assert (
            list(resolved.covers)
            == [stack.id for stack in stacks][:MAX_TIMELINE_STACK_MEMBER_READS]
        )
        (record,) = [r for r in caplog.records if hasattr(r, "member_read_cap")]
        assert getattr(record, "partial_stack_count") == total
        assert getattr(record, "member_read_cap") == MAX_TIMELINE_STACK_MEMBER_READS
        # The numerator is how many went uncollapsed, not how many were partial.
        # The ratio is the first thing triage reads, so a numerator equal to the
        # denominator would report a total outage on a bucket that in fact
        # resolved all but five.
        assert (
            getattr(record, "over_cap_stack_count")
            == total - MAX_TIMELINE_STACK_MEMBER_READS
        )
        # The stacks that went uncollapsed are the sample worth triaging, so it
        # is the tail past the cap rather than the front of the list.
        assert (
            getattr(record, "sample_stack_ids")
            == [stack.id for stack in stacks[MAX_TIMELINE_STACK_MEMBER_READS:]][:10]
        )

    @pytest.mark.anyio
    async def test_exactly_the_cap_reads_every_stack_and_stays_quiet(
        self, caplog: pytest.LogCaptureFixture
    ):
        """The boundary the sibling above doesn't reach.

        With `>=` instead of `>`, a bucket sitting exactly on the cap resolves
        every stack but still logs "above the cap; leaving 0 stacks uncollapsed"
        with an empty sample — a false alarm on every such request, on the
        endpoint the aggregate-record convention exists to keep quiet.
        """
        stacks, bucket, members_by_stack = [], [], {}
        for _ in range(MAX_TIMELINE_STACK_MEMBER_READS):
            stack, members = make_gumnut_stack_with_members(
                count=3, asset_count=3, primary_asset_id=None
            )
            stacks.append(stack)
            members_by_stack[stack.id] = members
            bucket.append(members[1])
        client = _timeline_client(stacks, members_by_stack=members_by_stack)

        with caplog.at_level(logging.WARNING):
            resolved = await resolve_timeline_stacks(client, bucket)

        assert client.assets.list.call_count == MAX_TIMELINE_STACK_MEMBER_READS
        assert len(resolved.covers) == MAX_TIMELINE_STACK_MEMBER_READS
        assert not [r for r in caplog.records if hasattr(r, "member_read_cap")]

    @pytest.mark.anyio
    async def test_bounds_concurrent_timeline_member_reads(self):
        """The cap above bounds the *total* reads; this bounds how many are open
        at once.

        The two are independent — a plain `asyncio.gather` still issues exactly
        `MAX_TIMELINE_STACK_MEMBER_READS` calls and satisfies every assertion
        above while opening all of them against the backend simultaneously, from
        a single request, on the timeline's hottest endpoint.
        """
        stacks, bucket, members_by_stack = [], [], {}
        for _ in range(BULK_FANOUT_CONCURRENCY_LIMIT * 3):
            stack, members = make_gumnut_stack_with_members(
                count=3, asset_count=3, primary_asset_id=None
            )
            stacks.append(stack)
            members_by_stack[stack.id] = members
            # One frame of each burst, so every stack lands partial and takes
            # the member read.
            bucket.append(members[1])
        tracker = _ConcurrencyTracker()
        client = _timeline_client(stacks)
        client.assets.list = Mock(
            side_effect=lambda **kwargs: _TrackedListing(
                members_by_stack[kwargs["stack_id"]], tracker
            )
        )

        await resolve_timeline_stacks(client, bucket)

        assert tracker.peak > 1, "expected concurrent member reads"
        assert tracker.peak <= BULK_FANOUT_CONCURRENCY_LIMIT

    @pytest.mark.anyio
    async def test_trashed_bucket_member_cannot_be_named_cover(self):
        """The fast path treats a bucket holding `asset_count` members as the
        stack's whole live set, and `asset_count` excludes trashed frames.

        Counting a trashed frame toward that total is the dangerous case: one
        trashed plus one live frame would total the row's count of 2, the
        earliest-frame rule would name the *trashed* one cover, and the live
        frame — the only one the timeline can show — would be collapsed away.
        Skipping trashed assets when grouping makes the totals disagree instead,
        routing the stack to the member read that resolves it correctly.

        This is also the one fixture where the row's `asset_count` and the
        number of members resolved for it genuinely differ (2 against 1), so it
        carries the count assertion: everywhere else the two are equal and
        emitting `len(members)` would pass.
        """
        stack, members = make_gumnut_stack_with_members(
            count=2, asset_count=2, trashed={0}, primary_asset_id=None
        )
        client = _timeline_client([stack], members_by_stack={stack.id: [members[1]]})

        resolved = await resolve_timeline_stacks(client, members)

        assert resolved.covers[stack.id] == members[1].id
        assert resolved.tuples[stack.id][1] == "2"
        client.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_stack_with_no_live_members_stays_unresolved(self):
        """`asset_count` is a live count, so zero means nothing to collapse to.

        Resolved on the fast path, not the fallback: zero live bucket members
        equals the row's zero count, so the stack classifies complete and the
        member read is never issued.
        """
        stack, members = make_gumnut_stack_with_members(
            count=2, asset_count=0, trashed={0, 1}
        )
        client = _timeline_client([stack])

        resolved = await resolve_timeline_stacks(client, members)

        assert resolved.covers == {}
        assert all(not resolved.is_collapsed_away(member) for member in members)
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_partial_stack_whose_member_read_comes_back_empty(self):
        """The fallback twin of the case above: the row claims live members but
        the read returns none, so there is still no frame to collapse to."""
        stack, members = make_gumnut_stack_with_members(
            count=2, asset_count=2, trashed={0, 1}
        )
        client = _timeline_client([stack], members_by_stack={stack.id: []})

        resolved = await resolve_timeline_stacks(client, members)

        assert resolved.covers == {}
        assert all(not resolved.is_collapsed_away(member) for member in members)
        client.assets.list.assert_called_once()

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
