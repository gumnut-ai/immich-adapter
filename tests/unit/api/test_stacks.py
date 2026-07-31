"""Tests for the stack routes in routers/api/stacks.py.

Cover resolution, member hydration, and DTO shape are pinned once in
`tests/unit/utils/test_stack_conversion.py`. These tests cover route calls,
pagination, filtering, write forwarding, and error responses.
"""

import inspect
import logging
import math
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException, status
from fastapi.routing import APIRoute
from gumnut import NotFoundError
from pydantic import ValidationError

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.api.stacks import (
    SEARCH_STACKS_CAP,
    SEARCH_STACKS_MEMBER_BUDGET,
    create_stack,
    get_stack,
    router,
    search_stacks,
    update_stack,
)
from routers.immich_models import StackCreateDto, StackUpdateDto
from routers.utils.current_user import get_current_user
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_stack_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_stack_id,
)
from tests.conftest import (
    MockPaginatedListing,
    MockSyncCursorPage,
    make_gumnut_asset,
    make_gumnut_stack_with_members,
    make_sdk_status_error,
)


def _client(*stacks_with_members) -> Mock:
    """A Mock client backing every stack read from the given (stack, members) pairs.

    `assets.list` routes on the `stack_id` filter so a multi-stack response
    can't pass by accident with one shared member list — a route that hydrated
    the wrong stack would get an empty member list and be dropped.
    """
    members_by_stack = {stack.id: members for stack, members in stacks_with_members}
    stacks_by_id = {stack.id: stack for stack, _ in stacks_with_members}

    client = Mock()
    client.stacks.list_stacks = Mock(
        return_value=MockSyncCursorPage([stack for stack, _ in stacks_with_members])
    )
    client.stacks.retrieve_stack = AsyncMock(
        side_effect=lambda stack_id: stacks_by_id[stack_id]
    )
    client.assets.list = Mock(
        side_effect=lambda **kwargs: MockSyncCursorPage(
            members_by_stack.get(kwargs["stack_id"], [])
        )
    )
    return client


def _search_log(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """The route's one per-request INFO summary.

    Filtered by level as well as logger name so an added WARNING (an oversized
    stack, a member-less one) can't be mistaken for the summary and silently
    turn every `stack_search_truncated` assertion into a lookup for an
    attribute the warning doesn't carry.
    """
    return next(
        record
        for record in caplog.records
        if record.name == "routers.api.stacks" and record.levelno == logging.INFO
    )


async def _search(client, current_user, primary_asset_id=None):
    return await search_stacks(  # type: ignore[call-arg]
        primaryAssetId=primary_asset_id,
        client=client,
        current_user=current_user,
    )


async def _get(client, current_user, stack_uuid):
    return await get_stack(  # type: ignore[call-arg]
        id=stack_uuid,
        client=client,
        current_user=current_user,
    )


def _write_client(stack, members) -> Mock:
    """Build a client whose stack writes return the supplied stack."""
    client = _client((stack, members))
    client.stacks.create_stack = AsyncMock(return_value=stack)
    client.stacks.set_cover = AsyncMock(return_value=stack)
    return client


async def _create(client, current_user, asset_uuids):
    return await create_stack(  # type: ignore[call-arg]
        request=StackCreateDto(assetIds=list(asset_uuids)),
        client=client,
        current_user=current_user,
    )


async def _update(client, current_user, stack_uuid, **body):
    """Call update with a literal body to distinguish omitted and null fields."""
    return await update_stack(  # type: ignore[call-arg]
        id=stack_uuid,
        request=StackUpdateDto(**body),
        client=client,
        current_user=current_user,
    )


def _member_uuids(members) -> list:
    return [safe_uuid_from_asset_id(member.id) for member in members]


class TestSearchStacks:
    @pytest.mark.anyio
    async def test_empty_library_returns_empty_list(self, mock_current_user):
        client = _client()

        assert await _search(client, mock_current_user) == []

    @pytest.mark.anyio
    async def test_returns_every_stack_with_hydrated_members(self, mock_current_user):
        first, first_members = make_gumnut_stack_with_members(count=2)
        second, second_members = make_gumnut_stack_with_members(count=3)
        client = _client((first, first_members), (second, second_members))

        result = await _search(client, mock_current_user)

        assert [stack.id for stack in result] == [
            safe_uuid_from_stack_id(first.id),
            safe_uuid_from_stack_id(second.id),
        ]
        assert [len(stack.assets) for stack in result] == [2, 3]
        assert [asset.id for asset in result[1].assets] == [
            safe_uuid_from_asset_id(member.id) for member in second_members
        ]

    @pytest.mark.anyio
    async def test_walks_past_one_page_of_stacks(self, mock_current_user):
        """`len(result)` is the real pin; the page count only shows the walk
        actually crossed a boundary instead of being satisfied by one oversized
        page.
        """
        total = GUMNUT_API_MAX_PAGE_SIZE + 5
        pairs = [make_gumnut_stack_with_members(count=1) for _ in range(total)]
        client = _client(*pairs)
        listings: list[MockPaginatedListing] = []

        def _list_stacks(**kwargs):
            listings.append(
                MockPaginatedListing(
                    [stack for stack, _ in pairs], page_size=kwargs["limit"]
                )
            )
            return listings[-1]

        client.stacks.list_stacks = Mock(side_effect=_list_stacks)

        result = await _search(client, mock_current_user)

        assert len(result) == total
        assert listings[0].pages_fetched == 2
        assert (
            client.stacks.list_stacks.call_args.kwargs["limit"]
            == GUMNUT_API_MAX_PAGE_SIZE
        )

    @pytest.mark.anyio
    async def test_pinned_cover_becomes_the_primary(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[2].id
        client = _client((stack, members))

        result = await _search(client, mock_current_user)

        assert result[0].primaryAssetId == safe_uuid_from_asset_id(members[2].id)
        assert result[0].assets[0].id == result[0].primaryAssetId

    @pytest.mark.anyio
    async def test_unpinned_burst_gets_a_synthesized_primary(self, mock_current_user):
        """An auto-detected burst has no pinned cover, but Immich's
        `primaryAssetId` is non-null — the first live member stands in."""
        stack, members = make_gumnut_stack_with_members(count=3, primary_asset_id=None)
        client = _client((stack, members))

        result = await _search(client, mock_current_user)

        assert result[0].primaryAssetId == safe_uuid_from_asset_id(members[0].id)

    @pytest.mark.anyio
    async def test_member_less_stack_is_omitted(self, mock_current_user):
        """A member-less row can't form a valid DTO, and dropping it must not
        drop its neighbours."""
        empty, _ = make_gumnut_stack_with_members(count=0)
        populated, members = make_gumnut_stack_with_members(count=2)
        client = _client((empty, []), (populated, members))

        result = await _search(client, mock_current_user)

        assert [stack.id for stack in result] == [safe_uuid_from_stack_id(populated.id)]

    @pytest.mark.anyio
    async def test_all_trashed_stack_is_omitted(self, mock_current_user):
        """It hydrates fine but converts to `assets: []` with a cover the client
        can't fetch, so it is dropped alongside the member-less case."""
        trashed, trashed_members = make_gumnut_stack_with_members(
            count=2, trashed={0, 1}
        )
        live, live_members = make_gumnut_stack_with_members(count=2)
        client = _client((trashed, trashed_members), (live, live_members))

        result = await _search(client, mock_current_user)

        assert [stack.id for stack in result] == [safe_uuid_from_stack_id(live.id)]

    @pytest.mark.anyio
    async def test_stops_walking_at_the_cap(
        self, mock_current_user, caplog: pytest.LogCaptureFixture
    ):
        """Without the break, the walk's cost has no ceiling — every stack past
        the cap is another full member read.

        `pages_fetched` is what distinguishes a real stop from a truncated
        result: slicing the walk's output to the cap would satisfy the length
        assertion while still having paid for the whole library.
        """
        oversized = SEARCH_STACKS_CAP + GUMNUT_API_MAX_PAGE_SIZE
        pairs = [make_gumnut_stack_with_members(count=1) for _ in range(oversized)]
        client = _client(*pairs)
        listing = MockPaginatedListing(
            [stack for stack, _ in pairs], page_size=GUMNUT_API_MAX_PAGE_SIZE
        )
        client.stacks.list_stacks = Mock(return_value=listing)

        with caplog.at_level(logging.INFO, logger="routers.api.stacks"):
            result = await _search(client, mock_current_user)

        assert len(result) == SEARCH_STACKS_CAP
        assert listing.pages_fetched == math.ceil(
            SEARCH_STACKS_CAP / GUMNUT_API_MAX_PAGE_SIZE
        )
        # Only the walked stacks cost a member read; the rest were never touched.
        assert client.assets.list.call_count == SEARCH_STACKS_CAP
        # The truncation is otherwise invisible — this log is the only signal a
        # library has outgrown the endpoint, so the flag is a contract.
        record = _search_log(caplog)
        assert getattr(record, "stack_search_truncated") is True
        assert getattr(record, "stack_truncated_by") == "stack_cap"
        assert getattr(record, "stacks_walked") == SEARCH_STACKS_CAP

    @pytest.mark.anyio
    async def test_exact_cap_is_not_reported_as_truncation(
        self, mock_current_user, caplog: pytest.LogCaptureFixture
    ):
        """The boundary the truncation flag has to get right.

        A library of exactly `SEARCH_STACKS_CAP` stacks is returned whole, so
        flagging it would tell an operator to reshape a read that answered in
        full — and the flag only earns its "this library outgrew the endpoint"
        meaning if it never fires when nothing was left out. Deriving it from
        `len(stacks) >= SEARCH_STACKS_CAP` after the walk is what breaks this.
        """
        pairs = [
            make_gumnut_stack_with_members(count=1) for _ in range(SEARCH_STACKS_CAP)
        ]
        client = _client(*pairs)

        with caplog.at_level(logging.INFO, logger="routers.api.stacks"):
            result = await _search(client, mock_current_user)

        assert len(result) == SEARCH_STACKS_CAP
        record = _search_log(caplog)
        assert getattr(record, "stack_search_truncated") is False
        assert getattr(record, "stack_truncated_by") is None

    @pytest.mark.anyio
    async def test_member_budget_stops_the_walk_before_the_stack_cap(
        self, mock_current_user, caplog: pytest.LogCaptureFixture
    ):
        """The bound the stack cap alone doesn't provide.

        These stacks are far below `SEARCH_STACKS_CAP` in number but carry
        enough members between them to blow the budget, so a walk bounded only
        by row count would hydrate every one. `asset_count` is set independently
        of the fixture's real member count on purpose: the budget is spent from
        the *listing row*, before anything is fetched, which is the whole reason
        it can bound work rather than measure it afterwards.
        """
        per_stack = SEARCH_STACKS_MEMBER_BUDGET // 5
        pairs = [
            make_gumnut_stack_with_members(count=2, asset_count=per_stack)
            for _ in range(20)
        ]
        client = _client(*pairs)

        with caplog.at_level(logging.INFO, logger="routers.api.stacks"):
            result = await _search(client, mock_current_user)

        assert len(result) == 5
        # The cap never came near binding — the budget is what stopped it.
        assert len(result) < SEARCH_STACKS_CAP
        assert client.assets.list.call_count == 5
        record = _search_log(caplog)
        assert getattr(record, "stack_search_truncated") is True
        assert getattr(record, "stack_truncated_by") == "member_budget"
        assert getattr(record, "stack_members_budgeted") == SEARCH_STACKS_MEMBER_BUDGET

    @pytest.mark.anyio
    async def test_exact_member_budget_is_not_reported_as_truncation(
        self, mock_current_user, caplog: pytest.LogCaptureFixture
    ):
        """The budget's half of the boundary that `stack_search_truncated` owes.

        Sibling of `test_exact_cap_is_not_reported_as_truncation`, and the
        assertion the budget test above cannot make: every one of its assertions
        also holds if the budget is spent *after* admitting, so only a library
        that lands exactly on the budget with the cursor exhausted separates the
        two orderings.
        """
        per_stack = SEARCH_STACKS_MEMBER_BUDGET // 5
        pairs = [
            make_gumnut_stack_with_members(count=2, asset_count=per_stack)
            for _ in range(5)
        ]
        client = _client(*pairs)

        with caplog.at_level(logging.INFO, logger="routers.api.stacks"):
            result = await _search(client, mock_current_user)

        assert len(result) == 5
        record = _search_log(caplog)
        assert getattr(record, "stack_members_budgeted") == SEARCH_STACKS_MEMBER_BUDGET
        assert getattr(record, "stack_search_truncated") is False
        assert getattr(record, "stack_truncated_by") is None

    @pytest.mark.anyio
    async def test_budgeted_and_hydrated_member_counts_diverge_on_trashed_members(
        self, mock_current_user, caplog: pytest.LogCaptureFixture
    ):
        """The one asymmetry the budget can't express, made observable.

        The budget is spent from the row's live `asset_count`, but
        `fetch_stack_members` reads `state="all"` — so trashed frames are
        hydrated and converted without ever being budgeted. That gap is why the
        summary reports both numbers; with only the budgeted figure, a library
        of heavily-trashed bursts would read as well inside its ceiling while
        costing a multiple of it.
        """
        stack, members = make_gumnut_stack_with_members(count=4, trashed={2, 3})
        assert stack.asset_count == 2, "the row counts live members only"
        client = _client((stack, members))

        with caplog.at_level(logging.INFO, logger="routers.api.stacks"):
            result = await _search(client, mock_current_user)

        # Only the live members reach the response — see `build_stack_response`.
        assert len(result[0].assets) == 2
        record = _search_log(caplog)
        assert getattr(record, "stack_members_budgeted") == 2
        assert getattr(record, "stack_members_hydrated") == 4

    @pytest.mark.anyio
    async def test_ordinary_library_logs_no_warning(
        self, mock_current_user, caplog: pytest.LogCaptureFixture
    ):
        """The happy path stays silent, or the oversized warning is just noise.

        Every other test in this class reads the summary through `_search_log`,
        which filters to INFO — so an `oversized` comparison flipped to `<`
        would warn on every request without failing anything here.
        """
        pairs = [make_gumnut_stack_with_members(count=3) for _ in range(3)]
        client = _client(*pairs)

        with caplog.at_level(logging.WARNING, logger="routers.api.stacks"):
            result = await _search(client, mock_current_user)

        assert len(result) == 3
        assert caplog.records == []

    @pytest.mark.anyio
    async def test_stack_larger_than_the_whole_budget_is_served_and_logged(
        self, mock_current_user, caplog: pytest.LogCaptureFixture
    ):
        """Admission is all-or-nothing, so one huge stack still hydrates whole.

        Truncating its members is the one thing the budget must not do —
        clients read `assets.length` as the stack's size — and dropping the
        user's largest stack would be a worse answer than a slow one. So the
        budget can be exceeded by up to one stack, and that case is surfaced by
        a warning rather than absorbed silently.
        """
        huge, huge_members = make_gumnut_stack_with_members(
            count=3, asset_count=SEARCH_STACKS_MEMBER_BUDGET + 1
        )
        client = _client((huge, huge_members))

        with caplog.at_level(logging.WARNING, logger="routers.api.stacks"):
            result = await _search(client, mock_current_user)

        assert [stack.id for stack in result] == [safe_uuid_from_stack_id(huge.id)]
        assert len(result[0].assets) == 3
        warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
        assert getattr(warning, "stack_id") == huge.id
        assert getattr(warning, "stack_members") == SEARCH_STACKS_MEMBER_BUDGET + 1

    @pytest.mark.anyio
    async def test_upstream_failure_reaches_the_global_handler(self, mock_current_user):
        """Backend errors are the global `GumnutError` handler's job, so the
        route must not swallow or rewrap them."""
        error = make_sdk_status_error(503, "backend down")
        client = _client()
        client.stacks.list_stacks = Mock(side_effect=error)

        with pytest.raises(type(error)):
            await _search(client, mock_current_user)

    @pytest.mark.anyio
    async def test_one_stack_failing_hydration_fails_the_request(
        self, mock_current_user
    ):
        """The deliberate choice `hydrate_stacks` leaves to its callers: a list
        that silently omits stacks a backend hiccup touched is indistinguishable
        from one where the user has fewer stacks."""
        failing, failing_members = make_gumnut_stack_with_members(count=1)
        healthy, healthy_members = make_gumnut_stack_with_members(count=1)
        client = _client((failing, failing_members), (healthy, healthy_members))
        error = make_sdk_status_error(503, "backend down")

        def _list(**kwargs):
            if kwargs["stack_id"] == failing.id:
                raise error
            return MockSyncCursorPage(healthy_members)

        client.assets.list = Mock(side_effect=_list)

        with pytest.raises(type(error)):
            await _search(client, mock_current_user)


class TestSearchStacksByPrimaryAsset:
    @pytest.mark.anyio
    async def test_finds_a_stack_by_its_pinned_cover(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[1].id
        client = _client((stack, members))
        client.assets.retrieve = AsyncMock(return_value=members[1])
        requested = safe_uuid_from_asset_id(members[1].id)

        result = await _search(client, mock_current_user, primary_asset_id=requested)

        assert [stack.id for stack in result] == [safe_uuid_from_stack_id(stack.id)]
        assert result[0].primaryAssetId == requested

    @pytest.mark.anyio
    async def test_finds_a_stack_by_its_synthesized_cover(self, mock_current_user):
        """The Gumnut API's own `primary_asset_id` filter matches only pinned
        covers, so forwarding it would miss every unpinned burst — the case this
        pins."""
        stack, members = make_gumnut_stack_with_members(count=3, primary_asset_id=None)
        client = _client((stack, members))
        client.assets.retrieve = AsyncMock(return_value=members[0])
        requested = safe_uuid_from_asset_id(members[0].id)

        result = await _search(client, mock_current_user, primary_asset_id=requested)

        assert [stack.id for stack in result] == [safe_uuid_from_stack_id(stack.id)]
        assert client.stacks.list_stacks.call_count == 0

    @pytest.mark.anyio
    async def test_non_primary_member_matches_nothing(self, mock_current_user):
        """The asset is in a stack, but isn't the frame Immich shows for it."""
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[0].id
        client = _client((stack, members))
        client.assets.retrieve = AsyncMock(return_value=members[2])

        result = await _search(
            client,
            mock_current_user,
            primary_asset_id=safe_uuid_from_asset_id(members[2].id),
        )

        assert result == []

    @pytest.mark.anyio
    async def test_unstacked_asset_matches_nothing(self, mock_current_user):
        client = _client()
        client.assets.retrieve = AsyncMock(return_value=make_gumnut_asset())

        result = await _search(client, mock_current_user, primary_asset_id=uuid4())

        assert result == []
        assert client.stacks.retrieve_stack.call_count == 0

    @pytest.mark.anyio
    async def test_unknown_asset_is_empty_not_404(self, mock_current_user):
        """A search filter naming a row that doesn't exist matches nothing —
        upstream's is a SQL equality filter, and the adapter's asset lookup is
        an implementation detail that shouldn't become the client's 404."""
        client = _client()
        client.assets.retrieve = AsyncMock(
            side_effect=make_sdk_status_error(404, "asset not found", cls=NotFoundError)
        )

        assert await _search(client, mock_current_user, primary_asset_id=uuid4()) == []

    @pytest.mark.anyio
    async def test_stack_deleted_before_the_stack_read_is_empty(
        self, mock_current_user
    ):
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _client((stack, members))
        client.assets.retrieve = AsyncMock(return_value=members[0])
        client.stacks.retrieve_stack = AsyncMock(
            side_effect=make_sdk_status_error(404, "stack not found", cls=NotFoundError)
        )

        result = await _search(
            client,
            mock_current_user,
            primary_asset_id=safe_uuid_from_asset_id(members[0].id),
        )

        assert result == []

    @pytest.mark.anyio
    async def test_stack_deleted_before_the_member_read_is_empty(
        self, mock_current_user
    ):
        """The member read has the widest window of the three, so it is the one
        most likely to lose the race — and it is inside the same guard."""
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _client((stack, members))
        client.assets.retrieve = AsyncMock(return_value=members[0])
        client.assets.list = Mock(
            side_effect=make_sdk_status_error(404, "stack not found", cls=NotFoundError)
        )

        result = await _search(
            client,
            mock_current_user,
            primary_asset_id=safe_uuid_from_asset_id(members[0].id),
        )

        assert result == []

    @pytest.mark.anyio
    async def test_all_trashed_stack_matches_nothing(self, mock_current_user):
        """The same drop rule the list and detail routes apply — a cover the
        client can't fetch is not a match — on the third path through it."""
        stack, members = make_gumnut_stack_with_members(count=2, trashed={0, 1})
        client = _client((stack, members))
        client.assets.retrieve = AsyncMock(return_value=members[0])

        result = await _search(
            client,
            mock_current_user,
            primary_asset_id=safe_uuid_from_asset_id(members[0].id),
        )

        assert result == []

    @pytest.mark.anyio
    async def test_stack_emptied_before_the_member_read_is_empty(
        self, mock_current_user
    ):
        """The same race with the opposite backend answer: the stack row is
        still there, but every member has left it, so there is no cover to
        compare against."""
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _client((stack, []))
        client.assets.retrieve = AsyncMock(return_value=members[0])

        result = await _search(
            client,
            mock_current_user,
            primary_asset_id=safe_uuid_from_asset_id(members[0].id),
        )

        assert result == []

    @pytest.mark.anyio
    async def test_asset_lookup_uses_the_asset_prefix(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _client((stack, members))
        client.assets.retrieve = AsyncMock(return_value=members[0])
        requested = safe_uuid_from_asset_id(members[0].id)

        await _search(client, mock_current_user, primary_asset_id=requested)

        assert client.assets.retrieve.call_args.args == (
            uuid_to_gumnut_asset_id(requested),
        )
        assert client.stacks.retrieve_stack.call_args.args == (stack.id,)


class TestGetStack:
    @pytest.mark.anyio
    async def test_returns_the_stack_with_its_cover_first(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[2].id
        client = _client((stack, members))
        stack_uuid = safe_uuid_from_stack_id(stack.id)

        result = await _get(client, mock_current_user, stack_uuid)

        assert result.id == stack_uuid
        assert result.primaryAssetId == safe_uuid_from_asset_id(members[2].id)
        assert [asset.id for asset in result.assets] == [
            safe_uuid_from_asset_id(members[2].id),
            safe_uuid_from_asset_id(members[0].id),
            safe_uuid_from_asset_id(members[1].id),
        ]

    @pytest.mark.anyio
    async def test_converts_the_uuid_with_the_stack_prefix(self, mock_current_user):
        """`asset_` is a strict prefix of `asset_stack_`; the wrong pair here
        would decode to a different entity's ID."""
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _client((stack, members))
        stack_uuid = safe_uuid_from_stack_id(stack.id)

        await _get(client, mock_current_user, stack_uuid)

        assert client.stacks.retrieve_stack.call_args.args == (
            uuid_to_gumnut_stack_id(stack_uuid),
        )

    @pytest.mark.anyio
    async def test_member_less_stack_is_404(self, mock_current_user):
        stack, _ = make_gumnut_stack_with_members(count=0)
        client = _client((stack, []))

        with pytest.raises(HTTPException) as exc_info:
            await _get(client, mock_current_user, safe_uuid_from_stack_id(stack.id))

        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_all_trashed_stack_is_404(self, mock_current_user):
        """The detail route drops on the same rule the list does, rather than
        serving an empty `assets` array with an unfetchable cover."""
        stack, members = make_gumnut_stack_with_members(count=2, trashed={0, 1})
        client = _client((stack, members))

        with pytest.raises(HTTPException) as exc_info:
            await _get(client, mock_current_user, safe_uuid_from_stack_id(stack.id))

        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_missing_or_foreign_stack_reaches_the_global_handler(
        self, mock_current_user
    ):
        """The Gumnut API answers "another user's stack" and "no such stack"
        identically, and the global handler turns both into Immich's 404."""
        error = make_sdk_status_error(404, "stack not found", cls=NotFoundError)
        client = _client()
        client.stacks.retrieve_stack = AsyncMock(side_effect=error)

        with pytest.raises(type(error)):
            await _get(client, mock_current_user, uuid4())


class TestCreateStack:
    @pytest.mark.anyio
    async def test_returns_the_new_stack_hydrated(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[0].id
        client = _write_client(stack, members)

        result = await _create(client, mock_current_user, _member_uuids(members))

        assert result.id == safe_uuid_from_stack_id(stack.id)
        assert result.primaryAssetId == safe_uuid_from_asset_id(members[0].id)
        assert [asset.id for asset in result.assets] == _member_uuids(members)

    @pytest.mark.anyio
    async def test_pins_the_first_asset_as_the_cover(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=3)
        client = _write_client(stack, members)
        requested = _member_uuids(members)

        await _create(client, mock_current_user, requested)

        kwargs = client.stacks.create_stack.call_args.kwargs
        assert kwargs["asset_ids"] == [
            uuid_to_gumnut_asset_id(asset_uuid) for asset_uuid in requested
        ]
        assert kwargs["primary_asset_id"] == uuid_to_gumnut_asset_id(requested[0])

    @pytest.mark.anyio
    async def test_omits_library_id(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _write_client(stack, members)

        await _create(client, mock_current_user, _member_uuids(members))

        assert "library_id" not in client.stacks.create_stack.call_args.kwargs

    @pytest.mark.anyio
    async def test_forwards_the_request_once_unmodified(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _write_client(stack, members)
        requested = _member_uuids(members)
        with_duplicate = [requested[0], requested[1], requested[0]]

        await _create(client, mock_current_user, with_duplicate)

        assert client.stacks.create_stack.call_count == 1
        assert client.stacks.create_stack.call_args.kwargs["asset_ids"] == [
            uuid_to_gumnut_asset_id(asset_uuid) for asset_uuid in with_duplicate
        ]

    def test_generated_dto_enforces_the_two_asset_minimum(self):
        """Keep the handler's first-item access protected by DTO validation."""
        with pytest.raises(ValidationError):
            StackCreateDto(assetIds=[uuid4()])

    def test_is_registered_as_201(self):
        route = next(
            r
            for r in router.routes
            if isinstance(r, APIRoute) and r.endpoint is create_stack
        )
        assert route.status_code == status.HTTP_201_CREATED

    @pytest.mark.anyio
    async def test_response_follows_the_backend_membership_not_the_request(
        self, mock_current_user
    ):
        """A backend merge can add members absent from the request."""
        stack, members = make_gumnut_stack_with_members(count=4)
        client = _write_client(stack, members)
        requested = _member_uuids(members[:2])

        result = await _create(client, mock_current_user, requested)

        assert [asset.id for asset in result.assets] == _member_uuids(members)
        assert client.stacks.create_stack.call_args.kwargs["asset_ids"] == [
            uuid_to_gumnut_asset_id(asset_uuid) for asset_uuid in requested
        ]

    @pytest.mark.anyio
    async def test_backend_rejection_propagates_with_nothing_read_back(
        self, mock_current_user
    ):
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _write_client(stack, members)
        error = make_sdk_status_error(404, "assets not found", cls=NotFoundError)
        client.stacks.create_stack = AsyncMock(side_effect=error)

        with pytest.raises(type(error)):
            await _create(client, mock_current_user, _member_uuids(members))

        assert client.assets.list.call_count == 0

    @pytest.mark.anyio
    async def test_all_trashed_stack_is_still_returned(
        self, mock_current_user, caplog: pytest.LogCaptureFixture
    ):
        """A successful write returns its ID even when all members are trashed."""
        stack, members = make_gumnut_stack_with_members(count=2, trashed={0, 1})
        client = _write_client(stack, members)

        with caplog.at_level(logging.WARNING, logger="routers.api.stacks"):
            result = await _create(client, mock_current_user, _member_uuids(members))

        assert result.id == safe_uuid_from_stack_id(stack.id)
        assert result.assets == []
        warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
        assert getattr(warning, "stack_id") == stack.id
        assert getattr(warning, "stack_asset_count") == stack.asset_count

    @pytest.mark.anyio
    async def test_member_less_read_back_is_an_error(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _write_client(stack, [])

        with pytest.raises(HTTPException) as exc_info:
            await _create(client, mock_current_user, _member_uuids(members))

        assert exc_info.value.status_code == 500


class TestUpdateStack:
    @pytest.mark.anyio
    async def test_pins_the_requested_cover_and_hydrates_the_result(
        self, mock_current_user
    ):
        stack, members = make_gumnut_stack_with_members(count=3)
        stack.primary_asset_id = members[2].id
        client = _write_client(stack, members)
        requested = safe_uuid_from_asset_id(members[2].id)

        result = await _update(
            client,
            mock_current_user,
            safe_uuid_from_stack_id(stack.id),
            primaryAssetId=requested,
        )

        assert result.primaryAssetId == requested
        assert [asset.id for asset in result.assets] == [
            requested,
            *_member_uuids(members[:2]),
        ]

    @pytest.mark.anyio
    async def test_converts_each_id_with_its_own_prefix(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _write_client(stack, members)
        stack_uuid = safe_uuid_from_stack_id(stack.id)
        cover_uuid = safe_uuid_from_asset_id(members[0].id)

        await _update(client, mock_current_user, stack_uuid, primaryAssetId=cover_uuid)

        assert client.stacks.set_cover.call_args.args == (
            uuid_to_gumnut_stack_id(stack_uuid),
        )
        assert client.stacks.set_cover.call_args.kwargs == {
            "primary_asset_id": uuid_to_gumnut_asset_id(cover_uuid)
        }

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "body", [{}, {"primaryAssetId": None}], ids=["omitted", "explicit-null"]
    )
    async def test_absent_cover_is_a_read(self, mock_current_user, body):
        stack, members = make_gumnut_stack_with_members(count=2)
        stack.primary_asset_id = members[1].id
        client = _write_client(stack, members)
        stack_uuid = safe_uuid_from_stack_id(stack.id)

        result = await _update(client, mock_current_user, stack_uuid, **body)

        assert client.stacks.set_cover.call_count == 0
        assert client.stacks.retrieve_stack.call_args.args == (
            uuid_to_gumnut_stack_id(stack_uuid),
        )
        assert result.primaryAssetId == safe_uuid_from_asset_id(members[1].id)

    @pytest.mark.anyio
    async def test_non_member_cover_propagates(self, mock_current_user):
        stack, members = make_gumnut_stack_with_members(count=2)
        client = _write_client(stack, members)
        error = make_sdk_status_error(400, "cover must be a member of the stack")
        client.stacks.set_cover = AsyncMock(side_effect=error)

        with pytest.raises(type(error)):
            await _update(
                client,
                mock_current_user,
                safe_uuid_from_stack_id(stack.id),
                primaryAssetId=uuid4(),
            )

    @pytest.mark.anyio
    async def test_missing_stack_propagates(self, mock_current_user):
        error = make_sdk_status_error(404, "stack not found", cls=NotFoundError)
        client = _client()
        client.stacks.retrieve_stack = AsyncMock(side_effect=error)

        with pytest.raises(type(error)):
            await _update(client, mock_current_user, uuid4())

    @pytest.mark.anyio
    @pytest.mark.parametrize("pins_a_cover", [False, True], ids=["read", "set-cover"])
    async def test_all_trashed_stack_is_404(self, mock_current_user, pins_a_cover):
        """Both read and set-cover paths apply the read response policy."""
        stack, members = make_gumnut_stack_with_members(count=2, trashed={0, 1})
        client = _write_client(stack, members)
        body = (
            {"primaryAssetId": safe_uuid_from_asset_id(members[0].id)}
            if pins_a_cover
            else {}
        )

        with pytest.raises(HTTPException) as exc_info:
            await _update(
                client, mock_current_user, safe_uuid_from_stack_id(stack.id), **body
            )

        assert exc_info.value.status_code == 404
        assert client.stacks.set_cover.call_count == (1 if pins_a_cover else 0)


class TestRouteDependencies:
    """Every stack route is user-scoped, so each must resolve a client and user.

    Unit tests call the handlers directly with explicit arguments, so nothing
    else here would notice a dependency going missing — and a `search_stacks`
    that lost `get_authenticated_gumnut_client` would serve stacks without
    authenticating.
    """

    @pytest.mark.parametrize(
        "handler", [search_stacks, get_stack, create_stack, update_stack]
    )
    @pytest.mark.parametrize(
        "param, dependency",
        [
            ("client", get_authenticated_gumnut_client),
            ("current_user", get_current_user),
        ],
    )
    def test_handler_declares_its_dependency(self, handler, param, dependency):
        default = inspect.signature(handler).parameters[param].default
        assert default.dependency is dependency
