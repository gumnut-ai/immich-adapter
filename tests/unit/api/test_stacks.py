"""Tests for the stack read routes in routers/api/stacks.py.

Cover resolution, member hydration, and DTO shape are pinned once in
`tests/unit/utils/test_stack_conversion.py`. What's left to these tests is what
the routes themselves decide: which upstream calls they make, how they exhaust
pagination, how the `primaryAssetId` filter is answered, and which conditions
become a 404 rather than a response.
"""

import inspect
import logging
import math
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from gumnut import NotFoundError

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.api.stacks import SEARCH_STACKS_CAP, get_stack, search_stacks
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
        empty, _ = make_gumnut_stack_with_members(count=0, asset_count=0)
        populated, members = make_gumnut_stack_with_members(count=2)
        client = _client((empty, []), (populated, members))

        result = await _search(client, mock_current_user)

        assert [stack.id for stack in result] == [safe_uuid_from_stack_id(populated.id)]

    @pytest.mark.anyio
    async def test_all_trashed_stack_is_omitted(self, mock_current_user):
        """It hydrates fine but converts to `assets: []` with a cover the client
        can't fetch, so it is dropped alongside the member-less case."""
        trashed, trashed_members = make_gumnut_stack_with_members(
            count=2, trashed={0, 1}, asset_count=0
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
        record = next(r for r in caplog.records if r.name == "routers.api.stacks")
        assert getattr(record, "stack_cap_hit") is True
        assert getattr(record, "stacks_walked") == SEARCH_STACKS_CAP

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
        stack, members = make_gumnut_stack_with_members(
            count=2, trashed={0, 1}, asset_count=0
        )
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
        stack, _ = make_gumnut_stack_with_members(count=0, asset_count=0)
        client = _client((stack, []))

        with pytest.raises(HTTPException) as exc_info:
            await _get(client, mock_current_user, safe_uuid_from_stack_id(stack.id))

        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_all_trashed_stack_is_404(self, mock_current_user):
        """The detail route drops on the same rule the list does, rather than
        serving an empty `assets` array with an unfetchable cover."""
        stack, members = make_gumnut_stack_with_members(
            count=2, trashed={0, 1}, asset_count=0
        )
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


class TestRouteDependencies:
    """The reads are user-scoped, so both must resolve a client and a user.

    Unit tests call the handlers directly with explicit arguments, so nothing
    else here would notice a dependency going missing — and a `search_stacks`
    that lost `get_authenticated_gumnut_client` would serve stacks without
    authenticating.
    """

    @pytest.mark.parametrize("handler", [search_stacks, get_stack])
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
