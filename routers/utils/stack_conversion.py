"""Shared translation from Gumnut burst stacks to Immich stack responses.

A Gumnut stack row carries only identity and counts — its members live on the
assets, reachable through the `stack_id` filter on `assets.list`. Every Immich
stack surface (the `/stacks` reads and writes, the timeline's per-asset stack
tuples, an asset's own `stack` block) therefore needs the same two steps before
it can answer: fetch the members, and decide which one Immich should show as
the cover. Both live here so the surfaces can't drift into disagreeing about
which frame represents a burst.

Member fetching is async and stack conversion is synchronous, deliberately: the
upstream reads are confined to `hydrate_stack` / `hydrate_stacks` so that
`convert_gumnut_asset_to_immich` stays an I/O-free pure conversion. Routes
resolve their stack context first and pass it into conversion, rather than the
converter growing a hidden round-trip for all of its existing callers.

Hydration is sized for the surfaces that must return the members themselves —
`StackResponseDto` carries the full `assets` array. Surfaces that need only an
ID, a count, and a cover (the timeline's `[stackId, assetCount]` tuples, an
asset's own `stack` block) should not reach for `hydrate_stack` reflexively: it
pulls every member with `ASSET_INCLUDE`, so on a timeline bucket spanning many
bursts they would pay metadata and people joins per frame and then discard all
but two values. Those surfaces want a leaner read, added when one has a caller.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from gumnut import AsyncGumnut
from gumnut.types.asset_response import AssetResponse

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.immich_models import StackResponseDto, UserResponseDto
from routers.utils.asset_conversion import ASSET_INCLUDE, convert_gumnut_asset_to_immich
from routers.utils.concurrency import gather_with_concurrency
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_stack_id,
)

logger = logging.getLogger(__name__)


class GumnutStackRow(Protocol):
    """The stack row shape every Gumnut stack endpoint returns.

    The SDK is generated per-operation, so `list_stacks`, `retrieve_stack`,
    `create_stack`, `add_assets_to_stack`, and `set_cover` each return a
    distinctly-named class carrying an identical field set. Matching the shape
    structurally lets one set of helpers serve all of them, and lets a future
    stack operation join without editing a union here.
    """

    @property
    def id(self) -> str: ...

    @property
    def asset_count(self) -> int: ...

    @property
    def primary_asset_id(self) -> str | None: ...


@dataclass(frozen=True)
class HydratedStack:
    """A Gumnut stack joined to its members, with an Immich cover resolved."""

    id: UUID
    """The stack's Immich-facing UUID."""

    primary_asset_id: UUID
    """The resolved cover — never null, unlike the Gumnut row's pinned cover.
    See `resolve_effective_primary` for how it is chosen."""

    members: Sequence[AssetResponse]
    """Every member, live and trashed, in the Gumnut API's ordering."""

    live_asset_count: int
    """The Gumnut row's own member count, which **excludes trashed members** and
    so can be smaller than `len(members)`. Carried through so callers filling
    Immich's `assetCount` fields pick between the two counts deliberately."""


async def fetch_stack_members(
    client: AsyncGumnut, gumnut_stack_id: str
) -> list[AssetResponse]:
    """Fetch every member of one stack, paging until the stack is exhausted.

    `state="all"` because Immich's stack DTOs must carry trashed members too:
    a pinned cover keeps its ID after being trashed, so a live-only read can
    omit the very asset the stack points at. `ASSET_INCLUDE` because each member
    is converted by `convert_gumnut_asset_to_immich`, which reads `metadata`,
    `people`, and the `file_data` scalars.

    `limit` is the per-page size, not a cap — `async for` walks the SDK's cursor
    pages, so a stack with more members than one page still returns in full.
    """
    return [
        asset
        async for asset in client.assets.list(
            stack_id=gumnut_stack_id,
            state="all",
            include=ASSET_INCLUDE,
            limit=GUMNUT_API_MAX_PAGE_SIZE,
        )
    ]


def resolve_effective_primary(
    stack: GumnutStackRow, members: Sequence[AssetResponse]
) -> AssetResponse | None:
    """Pick the member Immich shows as the stack's cover.

    Immich requires a non-null `primaryAssetId`, but a Gumnut auto-detected
    burst deliberately has none: the backend pins a cover only when a user picks
    one, leaving the default to the client. The adapter has to supply one
    anyway, so it resolves the choice here — once — rather than letting each
    surface invent its own and show a different frame for the same burst.

    In order:

    1. The pinned cover, when it is among the members. Kept even when trashed —
       the Gumnut API preserves a trashed cover's ID until the asset is
       permanently deleted, so discarding it the moment a user trashes that
       frame would silently override their explicit choice.
    2. Otherwise the first live member.
    3. Otherwise the first trashed member, so an all-trashed stack can still
       name a cover instead of becoming unrepresentable.

    A pinned ID that is *absent* from the members falls through to the same
    fallbacks — the asset left the stack between the two reads, and a cover
    pointing outside the stack is not a legal Immich response.

    "First" means first in whatever order the Gumnut API returns `state="all"`
    members; the adapter does not re-sort. The only property relied on is that
    the order is deterministic, so two reads of an unchanged stack resolve to
    the same cover.

    Returns `None` only when the stack has no members at all.
    """
    if not members:
        return None

    pinned_id = stack.primary_asset_id
    if pinned_id is not None:
        for member in members:
            if member.id == pinned_id:
                return member

    for member in members:
        if member.trashed_at is None:
            return member

    return members[0]


async def hydrate_stack(
    client: AsyncGumnut, stack: GumnutStackRow
) -> HydratedStack | None:
    """Fetch one stack's members and resolve its Immich cover.

    Returns `None` for a member-less stack. Such a row can't form a valid
    `StackResponseDto` — there is no honest `primaryAssetId` — so callers omit
    it from lists and 404 from detail reads. Manufacturing a UUID instead would
    only move the failure to the client, which would then fetch a cover asset
    that doesn't exist.
    """
    members = await fetch_stack_members(client, stack.id)
    primary = resolve_effective_primary(stack, members)
    if primary is None:
        # The row's own count makes the disagreement queryable: `asset_count`
        # above zero means the row and the member read contradict each other —
        # a backend inconsistency, or a `stack_id` filter matching nothing. Zero
        # is ambiguous rather than reassuring, since the count excludes trashed
        # members: a dissolved stack and an all-trashed one whose member read
        # came back empty look identical here.
        logger.warning(
            "Stack %s has no members; omitting it from Immich responses",
            stack.id,
            extra={"stack_id": stack.id, "stack_asset_count": stack.asset_count},
        )
        return None

    return HydratedStack(
        id=safe_uuid_from_stack_id(stack.id),
        primary_asset_id=safe_uuid_from_asset_id(primary.id),
        members=members,
        live_asset_count=stack.asset_count,
    )


async def hydrate_stacks(
    client: AsyncGumnut, stacks: Sequence[GumnutStackRow]
) -> list[HydratedStack | None]:
    """Hydrate a collection of stacks under the shared fan-out bound.

    Each stack costs its own `assets.list` walk, so a full page of stacks would
    open that many concurrent upstream reads if launched naively.
    `gather_with_concurrency` bounds the in-flight calls and preserves input
    order, so the result zips back to `stacks` positionally — including the
    `None` entries for member-less stacks.

    An upstream failure on any one stack aborts the whole batch: the exception
    propagates and the partial results are discarded. The siblings are *not*
    cancelled — `asyncio.gather` lets them run to completion — so an aborted
    batch still costs its full member fan-out. Only a route knows whether its
    endpoint should fail or degrade, so a caller wanting to drop just the failed
    stack (say, one deleted between the listing page and its hydration) should
    catch per stack rather than change this for everyone. Note the asymmetry
    with a member-less stack, which is *not* an upstream failure and yields
    `None`.
    """
    return await gather_with_concurrency(
        [hydrate_stack(client, stack) for stack in stacks]
    )


def build_stack_response(
    hydrated: HydratedStack, current_user: UserResponseDto
) -> StackResponseDto:
    """Convert an already-hydrated stack into Immich's `StackResponseDto`."""
    return StackResponseDto(
        id=hydrated.id,
        primaryAssetId=hydrated.primary_asset_id,
        assets=[
            convert_gumnut_asset_to_immich(member, current_user)
            for member in hydrated.members
        ],
    )
