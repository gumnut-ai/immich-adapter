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

Hydration comes in two sizes, because the surfaces do. `hydrate_stack` pulls
every member with `ASSET_INCLUDE`, for the `/stacks` reads that must return the
member assets themselves. Surfaces needing only an ID, a count, and a cover —
an asset's own nested `stack` block, and eventually the timeline's
`[stackId, assetCount]` tuples — go through `resolve_asset_stack_summaries`
instead, which never fetches a member the response won't contain.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import batched
from typing import Protocol
from uuid import UUID

from gumnut import AsyncGumnut
from gumnut.types.asset_response import AssetResponse

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS, GUMNUT_API_MAX_PAGE_SIZE
from routers.immich_models import (
    AssetResponseDto,
    AssetStackResponseDto,
    StackResponseDto,
    UserResponseDto,
)
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
    """Every member, live and trashed, in capture order — trashed ones so a
    trashed pin stays resolvable. `build_stack_response` decides what reaches
    the response."""

    live_asset_count: int
    """The Gumnut row's own member count, which **excludes trashed members** and
    so can be smaller than `len(members)`.

    The timeline tuple and an asset's `stack` block read this count, while
    `StackResponseDto` has none — see `build_stack_response`. The row and the
    member read are taken at different instants, so a frame trashed in between
    makes them disagree by one; pick per surface deliberately."""


async def fetch_stack_members(
    client: AsyncGumnut, gumnut_stack_id: str
) -> list[AssetResponse]:
    """Fetch every member of one stack, paging until the stack is exhausted.

    `state="all"` so cover resolution can see a trashed pin: a pinned cover
    keeps its ID after being trashed, so a live-only read can omit the very
    asset the stack points at. `ASSET_INCLUDE` because each member is converted
    by `convert_gumnut_asset_to_immich`, which reads `metadata`, `people`, and
    the `file_data` scalars.

    `order="asc"` is passed rather than inherited: the server default is newest
    first, which would make an unpinned burst's cover its *last* frame. Pinning
    it here makes `resolve_effective_primary`'s "first live member" the earliest
    frame — matching Immich, where the first asset of a `POST /stacks` becomes
    the primary — and keeps a future change to the server default from silently
    moving the cover of every auto-detected burst.

    `limit` is the per-page size, not a cap — `async for` walks the SDK's cursor
    pages, so a stack with more members than one page still returns in full.
    """
    return [
        asset
        async for asset in client.assets.list(
            stack_id=gumnut_stack_id,
            state="all",
            order="asc",
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
       frame would silently override their explicit choice. Upstream Immich
       does the same.
    2. Otherwise the first live member — the earliest frame of the burst, since
       `fetch_stack_members` pins `order="asc"`.
    3. Otherwise the first trashed member, so an all-trashed stack can still
       name a cover instead of becoming unrepresentable.

    A pinned ID that is *absent* from the members falls through to the same
    fallbacks — the asset left the stack between the two reads. `hydrate_stack`
    logs that case, since it silently replaces a cover the user chose.

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

    pinned_id = stack.primary_asset_id
    if pinned_id is not None and primary.id != pinned_id:
        # The row named a cover the member read didn't return — the same
        # row-vs-member disagreement as above, but this one is user-visible:
        # the burst gets a different cover than the one the user pinned.
        logger.warning(
            "Stack %s pinned cover %s is not among its members; falling back to %s",
            stack.id,
            pinned_id,
            primary.id,
            extra={
                "stack_id": stack.id,
                "pinned_asset_id": pinned_id,
                "effective_asset_id": primary.id,
            },
        )

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

    The bound is on concurrency only — round-trips and peak memory still scale
    with `len(stacks)` times each stack's member count, and neither factor is
    capped here. Bounding both is the caller's job, and it is cheap to do
    because a listing row already carries its own `asset_count`: budget the
    members before hydrating rather than discovering the total afterwards (see
    `SEARCH_STACKS_CAP` / `SEARCH_STACKS_MEMBER_BUDGET` in
    `routers/api/stacks.py`). Immich's `searchStacks` takes no pagination
    parameters, so a whole-library read must answer with every stack at once; a
    library that keeps hitting those bounds wants the transpose instead, one
    `assets.list` walk grouped in memory by `stack_id`. This helper suits a
    bounded set of stacks.

    An upstream failure on any one stack aborts the whole batch, and the
    siblings still run to completion — see `gather_with_concurrency`. Only a
    route knows whether its endpoint should fail or degrade, so a caller wanting
    to drop just the failed stack (say, one deleted between the listing page and
    its hydration) should catch per stack rather than change this for everyone.
    Note the asymmetry with a member-less stack, which is *not* an upstream
    failure and yields `None`.
    """
    return await gather_with_concurrency(
        [hydrate_stack(client, stack) for stack in stacks]
    )


def build_stack_response(
    hydrated: HydratedStack, current_user: UserResponseDto
) -> StackResponseDto:
    """Convert an already-hydrated stack into Immich's `StackResponseDto`.

    Two shape rules the field types don't express, both copied from upstream
    Immich so clients written against it behave:

    - **`assets` carries live members only.** `StackResponseDto` has no count
      field, so clients read `assets.length` as the stack's size — including
      into the timeline, which counts live assets. Trashed frames here would
      inflate that badge and feed already-trashed IDs to "keep this, delete
      others".
    - **`assets[0]` is the primary**, when the primary is live. Clients depend
      on the position: the asset viewer jumps to `assets[0]` after removing an
      asset from a stack, and the filmstrip renders in server order.

    Together these mean a *trashed* pin names an asset absent from `assets` —
    upstream's behavior too — so callers must not assume `primaryAssetId` can
    be found there.
    """
    # No `stack_summaries` — a stack's own members deliberately carry a null
    # nested `stack` block. Upstream's `mapStack` maps them with `mapAsset(asset,
    # { auth })` and no `withStack`, so every client is written against the
    # absence; filling it in would nest each member's summary inside the very
    # stack it describes, for a field no reader of this response consults.
    live_assets = [
        convert_gumnut_asset_to_immich(member, current_user)
        for member in hydrated.members
        if member.trashed_at is None
    ]
    return StackResponseDto(
        id=hydrated.id,
        primaryAssetId=hydrated.primary_asset_id,
        # Stable, so the cover moves to the front and the rest keep capture
        # order — the same partition upstream's `mapStack` performs.
        assets=sorted(live_assets, key=lambda a: a.id != hydrated.primary_asset_id),
    )


async def fetch_stack_cover_candidates(
    client: AsyncGumnut, gumnut_stack_id: str
) -> list[AssetResponse]:
    """Fetch one stack's members for cover resolution only.

    The same walk as `fetch_stack_members` — `state="all"` so a trashed pin
    stays resolvable, `order="asc"` so "first live member" means the earliest
    frame — minus `ASSET_INCLUDE`. `resolve_effective_primary` reads only `id`
    and `trashed_at`, both lean-core fields that stay populated with no include
    token (see `ASSET_INCLUDE`'s comment for the lean-default migration this
    rides on). Nothing downstream converts these rows, so paying for `metadata`,
    `people`, and `file_data` on every frame of every burst on a search page
    would buy nothing.

    Kept as a sibling of `fetch_stack_members` rather than an `include`
    parameter on it: the two differ in what the caller does with the result, and
    an `include` argument threaded through would make it easy to hand a
    lean-fetched member to a converter that needs the heavy fields.
    """
    return [
        asset
        async for asset in client.assets.list(
            stack_id=gumnut_stack_id,
            state="all",
            order="asc",
            limit=GUMNUT_API_MAX_PAGE_SIZE,
        )
    ]


async def resolve_stack_cover(
    client: AsyncGumnut, stack: GumnutStackRow
) -> UUID | None:
    """Resolve a stack's Immich cover without hydrating its members.

    Same answer as `HydratedStack.primary_asset_id`, reached more cheaply:

    - **A pinned cover is taken at its word**, with no member read at all. This
      is the one place the summary path can disagree with `hydrate_stack`, which
      verifies the pin is among the members and falls back (loudly) when it
      isn't. That disagreement needs an asset to have left the stack while its
      pin lingered — a backend inconsistency `hydrate_stack` already logs from
      the `/stacks` routes — and checking for it here would cost a member read
      on every pinned stack of every search page to correct a case that should
      not occur.
    - **An unpinned stack** — the normal shape for an auto-detected burst, which
      the backend never pins a cover for — runs `resolve_effective_primary` over
      a lean member read. Calling that helper verbatim, rather than
      re-implementing "first live member" here, is what keeps this path and
      `/stacks` from ever naming different frames as the same burst's cover.

    Returns `None` only when the member read comes back empty, which for an
    unpinned stack means it has no members at all.
    """
    pinned_id = stack.primary_asset_id
    if pinned_id is not None:
        return safe_uuid_from_asset_id(pinned_id)

    members = await fetch_stack_cover_candidates(client, stack.id)
    primary = resolve_effective_primary(stack, members)
    if primary is None:
        # Same member-less contradiction `hydrate_stack` warns about, reached
        # from the summary path: the row exists but its members don't.
        logger.warning(
            "Stack %s has no members; omitting its asset stack summary",
            stack.id,
            extra={"stack_id": stack.id, "stack_asset_count": stack.asset_count},
        )
        return None
    return safe_uuid_from_asset_id(primary.id)


def build_asset_stack_summary(
    stack: GumnutStackRow, cover_asset_id: UUID
) -> AssetStackResponseDto:
    """Build the nested `AssetResponseDto.stack` block for one stack.

    `assetCount` is the row's **live** member count, which is what the field
    means to a client: web renders it directly as the burst badge's number, and
    it has to agree with the `assets.length` a follow-up `GET /api/stacks/{id}`
    reports, since `StackResponseDto` carries live members only (see
    `build_stack_response`). Trashed frames counted here would show a badge of
    5 on a stack whose filmstrip holds 3.
    """
    return AssetStackResponseDto(
        id=safe_uuid_from_stack_id(stack.id),
        primaryAssetId=cover_asset_id,
        assetCount=stack.asset_count,
    )


async def resolve_asset_stack_summaries(
    client: AsyncGumnut, gumnut_assets: Sequence[AssetResponse]
) -> dict[str, AssetStackResponseDto]:
    """Resolve the nested stack summary for every stacked asset in one batch.

    Returns a lookup keyed by **Gumnut** stack ID (the `asset_stack_`-prefixed
    form on `AssetResponse.stack_id`), ready to hand to
    `convert_gumnut_asset_to_immich`. An asset whose `stack_id` is absent from
    the lookup — because it is null, or because its stack didn't resolve —
    converts to `stack=None`; the converter treats both alike and stays
    I/O-free.

    The work is shaped by what the backend offers. Stack rows come back in
    chunked `list_stacks(ids=...)` calls because that filter accepts a bounded
    id list, so N stacked assets sharing one stack cost one row lookup rather
    than N. Covers cannot be batched the same way — `assets.list` takes a
    singular `stack_id` and there is no multi-stack filter — so each stack still
    needing a cover costs its own read, run under `gather_with_concurrency`.
    Pinned stacks and zero-member stacks need none at all, which is why the
    common auto-burst-heavy page costs one row read plus one cover read per
    distinct burst, not per asset.

    Nothing here bounds total round-trips, only concurrency. That is deliberate
    for now: every caller's page size is already capped upstream (search at the
    Gumnut per-page ceiling, memories at 30 windows x 20 assets), so the
    unpinned-stack count is bounded by those rather than by the library. The
    summary log below records how many stacks were seen against how many needed
    a cover read, which is the measurement that would justify adding a budget
    like `SEARCH_STACKS_MEMBER_BUDGET` rather than guessing at one.

    An upstream failure on any single stack fails the whole call — the same
    all-or-nothing `hydrate_stacks` has, and the same reasoning: a page that
    silently drops the stack block on the assets a backend hiccup touched is
    indistinguishable, to the client, from those assets not being stacked.
    """
    # Deduped and order-preserving, so the log and any future budget count
    # distinct stacks rather than stacked assets, and a fixed input yields a
    # fixed sequence of upstream calls.
    stack_ids = list(
        dict.fromkeys(
            asset.stack_id for asset in gumnut_assets if asset.stack_id is not None
        )
    )
    if not stack_ids:
        # The overwhelmingly common page: nothing stacked, so no stack API call
        # is made at all. Every caller runs through this helper unconditionally,
        # which only stays acceptable because of this early return.
        return {}

    rows: dict[str, GumnutStackRow] = {}
    for chunk in batched(stack_ids, GUMNUT_API_MAX_BULK_IDS):
        async for row in client.stacks.list_stacks(
            ids=list(chunk), limit=GUMNUT_API_MAX_PAGE_SIZE
        ):
            rows[row.id] = row

    dangling = [stack_id for stack_id in stack_ids if stack_id not in rows]
    if dangling:
        # One warning for the batch, not one per asset: a stack deleted between
        # the asset read and this one shows up on every frame it held, and a
        # per-asset warning would turn one backend event into a burst of
        # identical lines.
        logger.warning(
            "%d stack id(s) on assets resolved to no stack row; "
            "those assets ship without a stack summary",
            len(dangling),
            extra={"dangling_stack_ids": dangling},
        )

    # Zero live members is the same "not representable" state `/stacks` drops a
    # stack for (`_build_representable_response`): its `StackResponseDto` would
    # carry an empty `assets`. Emitting the summary anyway would badge the asset
    # with a count of 0 and hand the client a stack ID whose `GET
    # /api/stacks/{id}` 404s — a control that appears and then fails. Filtered
    # before the cover reads, so an all-trashed burst costs nothing.
    resolvable = [row for row in rows.values() if row.asset_count > 0]
    cover_reads = sum(1 for row in resolvable if row.primary_asset_id is None)
    covers = await gather_with_concurrency(
        [resolve_stack_cover(client, row) for row in resolvable]
    )

    summaries = {
        row.id: build_asset_stack_summary(row, cover)
        for row, cover in zip(resolvable, covers)
        if cover is not None
    }
    logger.info(
        "asset stack summaries: %d stacked asset(s) across %d stack(s), "
        "%d cover read(s), %d summary(ies) resolved",
        sum(1 for asset in gumnut_assets if asset.stack_id is not None),
        len(stack_ids),
        cover_reads,
        len(summaries),
        extra={
            "stack_summary_stacks": len(stack_ids),
            "stack_summary_cover_reads": cover_reads,
            "stack_summary_resolved": len(summaries),
            "stack_summary_dangling": len(dangling),
        },
    )
    return summaries


async def convert_assets_with_stacks(
    client: AsyncGumnut,
    gumnut_assets: Sequence[AssetResponse],
    current_user: UserResponseDto,
) -> list[AssetResponseDto]:
    """Convert a page of assets to Immich DTOs with their stack summaries filled.

    The default entry point for a REST route emitting `AssetResponseDto`s: one
    batched stack resolution for the whole page, then the ordinary synchronous
    conversion per asset. Output order matches the input.

    A route that emits assets in *groups* (memories, whose response nests assets
    under each synthesized memory) should call `resolve_asset_stack_summaries`
    once over the flattened set and pass the lookup down instead — calling this
    per group would re-read the same stack rows for every group.
    """
    stack_summaries = await resolve_asset_stack_summaries(client, gumnut_assets)
    return [
        convert_gumnut_asset_to_immich(
            asset, current_user, stack_summaries=stack_summaries
        )
        for asset in gumnut_assets
    ]
