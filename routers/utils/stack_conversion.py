"""Shared translation from Gumnut burst stacks to Immich stack responses.

A Gumnut stack row carries only identity and counts — its members live on the
assets, reachable through the `stack_id` filter on `assets.list`. Every Immich
stack surface (the `/stacks` reads and writes, the timeline's per-asset stack
tuples, an asset's own `stack` block) therefore has to decide which frame Immich
shows as the burst's cover, and most of them have to read the members to do it —
a pinned row being the one shape that answers from its own field. Both the read
and the choice live here so the surfaces can't drift into disagreeing about
which frame represents a burst.

Member fetching is async and stack conversion is synchronous, deliberately: the
upstream reads are confined to the async helpers in this module — `hydrate_stack`
/ `hydrate_stacks` for the surfaces that return the members themselves, and
`resolve_asset_stack_summaries` / `resolve_stack_cover` for those that need only
a cover — so that `convert_gumnut_asset_to_immich` stays an I/O-free pure
conversion. Routes resolve their stack context first and pass it into conversion,
rather than the converter growing a hidden round-trip for all of its existing
callers.

Hydration comes in two sizes, because the surfaces do. `hydrate_stack` pulls
every member with `ASSET_INCLUDE`, for the `/stacks` reads that must return the
member assets themselves. Surfaces needing only an ID, a count, and a cover go
through a leaner read instead, one that never fetches a member the response
won't contain: an asset's own nested `stack` block through
`resolve_asset_stack_summaries`, and the timeline's `[stackId, assetCount]`
tuples through `resolve_timeline_stacks`.

The two sizes also fail differently, and deliberately. A `/stacks` read is
*about* the stack, so `hydrate_stacks` lets an upstream error propagate. A lean
read is decoration on a response that is about the assets, so those paths
degrade rather than take the whole page down with it — at whichever granularity
the failed read allows. A failed cover read costs one stack its summary
(`resolve_stack_cover`); a failed stack-row lookup costs the page all of them
(`resolve_asset_stack_summaries`); an unresolvable timeline stack simply stays
uncollapsed (`resolve_timeline_stacks`).
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import batched
from typing import Protocol
from uuid import UUID

from gumnut import AsyncGumnut, GumnutError
from gumnut.types.asset_response import AssetResponse

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS, GUMNUT_API_MAX_PAGE_SIZE
from routers.immich_models import (
    AssetResponseDto,
    AssetStackResponseDto,
    StackResponseDto,
    UserResponseDto,
)
from routers.utils.asset_conversion import (
    ASSET_INCLUDE,
    convert_gumnut_asset_to_immich,
    resolve_capture_datetime,
)
from routers.utils.concurrency import gather_with_concurrency
from routers.utils.datetime_utils import to_actual_utc
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_stack_id,
)

logger = logging.getLogger(__name__)

# Ceiling on the cover reads one request will spend filling nested asset stack
# summaries. Only unpinned stacks cost a read (a pinned row names its own
# cover), and each is a separate round trip because `assets.list` takes a
# singular `stack_id` — there is no multi-stack filter to batch them with.
#
# Sized against the widest caller: `/search/random` admits up to 1000 assets in
# one response, so without a bound a burst-heavy library could queue 1000 reads
# behind `BULK_FANOUT_CONCURRENCY_LIMIT`, or 100 sequential waves, for a
# decorative badge. 100 keeps the worst case to ~10 waves while covering any
# realistic page: a 200-asset search page would have to be made almost entirely
# of distinct 2-frame bursts to reach it.
STACK_SUMMARY_COVER_READ_BUDGET = 100

# Cap on the per-request fallback member reads a collapsed timeline bucket will
# issue (see `resolve_timeline_stacks`). `gather_with_concurrency` bounds
# in-flight calls but not the total, and a filtered bucket can leave every stack
# in the month partial, so without a cap one inbound request could fan out into
# hundreds. Stacks past the cap stay uncollapsed rather than uncounted — the
# same inert degradation as a missing row. Sized well above the handful of
# month-straddling bursts the main timeline actually produces.
#
# The row read a step earlier needs no such cap despite looking symmetric: it is
# one request per `GUMNUT_API_MAX_BULK_IDS` stacks, so N stacks costs
# ceil(N / 200) requests rather than N.
MAX_TIMELINE_STACK_MEMBER_READS = 50


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


async def fetch_stack_cover_prefix(
    client: AsyncGumnut, gumnut_stack_id: str
) -> list[AssetResponse]:
    """Read an **unpinned** stack's members only as far as its cover.

    Returns a prefix of the member list, not the member set: the walk stops at
    the first live member, because that member *is* an unpinned stack's cover
    (`resolve_effective_primary` rule 2) and nothing after it can change the
    answer. A stack whose members are all trashed has no live member to stop on,
    so it walks to exhaustion — which is exactly the signal the caller needs to
    tell that case apart.

    Only correct for a stack with no pinned cover. A pinned one can name any
    member, including the last, so truncating its read could lose the pin and
    silently demote it to the fallback. `resolve_stack_cover` is the only
    caller and it short-circuits pinned rows before reaching here.

    The walk otherwise matches `fetch_stack_members` — `state="all"` so a
    trashed member still counts toward "this stack exists", `order="asc"` so
    "first live member" means the earliest frame — minus `ASSET_INCLUDE`.
    `resolve_effective_primary` reads only `id` and `trashed_at`, both
    lean-core fields that stay populated with no include token (see
    `ASSET_INCLUDE`'s comment for the lean-default migration this rides on).
    Nothing downstream converts these rows, so paying for `metadata`, `people`,
    and `file_data` on every frame of every burst on a search page would buy
    nothing. `test_cover_walk_matches_the_member_walk` pins that shared
    `state`/`order` pair across both functions — the duplication is two entry
    points, not two independent policies.

    Kept as a sibling of `fetch_stack_members` rather than an `include`
    parameter on it: the two now differ in *how far they read*, not just what
    they request, and an `include` argument threaded through would make it easy
    to hand a lean, truncated member list to a converter that needs neither.
    """
    members: list[AssetResponse] = []
    async for asset in client.assets.list(
        stack_id=gumnut_stack_id,
        state="all",
        order="asc",
        limit=GUMNUT_API_MAX_PAGE_SIZE,
    ):
        members.append(asset)
        if asset.trashed_at is None:
            break
    return members


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

    Returns `None` when the stack has nothing a client could act on — no members
    at all, or no *live* member — and also when the cover read itself fails,
    which is logged and costs only this stack its summary rather than the page's
    (see the `except` below). A caller inheriting this path inherits that
    swallowed-failure posture, which is the opposite of `hydrate_stack`'s.

    The no-live-member case is where the member read earns
    its keep over the row's `asset_count`. Both describe "no live frames", but
    the count is read from a different endpoint at a different instant, so a
    count that hasn't caught up with a just-trashed member would let the stack
    through — and `resolve_effective_primary` would then hand back a trashed
    frame (its rule 3), producing a summary whose `GET /api/stacks/{id}` 404s.
    That is the "control that appears and then fails" the caller's own filter is
    trying to prevent. Deciding it from the members instead makes this path
    agree with `_build_representable_response` by construction rather than by
    two endpoints agreeing. Note this is deliberately *not* rule 3: a stack with
    no live frames is unrepresentable as a summary, where `/stacks` merely drops
    it. The pinned branch genuinely cannot tell without a read, which is the
    trade-off documented above.
    """
    pinned_id = stack.primary_asset_id
    if pinned_id is not None:
        return safe_uuid_from_asset_id(pinned_id)

    try:
        members = await fetch_stack_cover_prefix(client, stack.id)
    except GumnutError:
        # Caught per stack rather than left to the batch: cover reads are one
        # round trip each, so a transient failure on one burst would otherwise
        # take every other stack's summary on the page down with it — see
        # `gather_with_concurrency`, which discards partial results. This stack
        # alone loses its badge.
        logger.warning(
            "Cover read failed for stack %s; omitting its asset stack summary",
            stack.id,
            extra={"stack_id": stack.id},
            exc_info=True,
        )
        return None

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
    if primary.trashed_at is not None:
        # Unpinned, so `resolve_effective_primary` returned a trashed frame only
        # after finding no live one — see the docstring. Not logged: trashing
        # every frame of a burst is ordinary user action, and the row count
        # normally filters this out before a read is ever spent.
        return None
    return safe_uuid_from_asset_id(primary.id)


async def resolve_asset_stack_summaries(
    client: AsyncGumnut, gumnut_assets: Sequence[AssetResponse]
) -> dict[str, AssetStackResponseDto]:
    """Resolve stack summaries for a page, degrading to `{}` on upstream failure.

    `_resolve_asset_stack_summaries` does the resolving; this wrapper owns the
    failure posture, which is the half worth explaining.

    **A stack read that fails degrades the page rather than failing it.** The
    block is decoration on a response whose real payload is the assets: losing a
    burst badge costs a client nothing it can't recover on the next read, while
    failing the request costs it the assets too. That matters more than it
    sounds, because the global `GumnutError` handler forwards an upstream status
    verbatim — a 404 from *this* lookup on `GET /api/assets/{id}` would reach
    Immich web as "that asset is gone" and close the viewer on an asset that
    exists. It also keeps this helper from overriding a caller's own posture:
    `search_memories` deliberately tolerates a failed year (see
    `_gather_year_assets`), and an all-or-nothing summary bolted onto it would
    have made a cosmetic badge fail the whole carousel. Contrast
    `hydrate_stacks`, which is right to fail loudly — there the stack *is* the
    response.

    `GumnutError` is the SDK's base class, so this covers both an HTTP status
    from the stacks/assets reads and a transport failure. Nothing else is caught
    — an `AttributeError` or a DTO validation failure here is an adapter bug, and
    swallowing it would turn a broken stack summary into a silently absent one on
    every response rather than a visible 500.
    """
    try:
        return await _resolve_asset_stack_summaries(client, gumnut_assets)
    except GumnutError:
        logger.warning(
            "Stack summary resolution failed; the page ships without stack blocks",
            exc_info=True,
        )
        return {}


async def _resolve_asset_stack_summaries(
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

    Concurrency alone would not bound the work, because the callers' page sizes
    don't agree on a ceiling: `/search/metadata`, `/search/smart`, and the
    criterion-less listing page clamp to the Gumnut per-page ceiling (200) and
    memories tops out at 30 windows x 20 assets, but `/search/random` is capped
    only by `RandomSearchDto.size` — up to **1000** assets, and so up to 1000
    distinct unpinned bursts, in one request. `STACK_SUMMARY_COVER_READ_BUDGET`
    bounds the reads that page can commit to; stacks past it ship `stack=None`,
    the same degraded shape a dangling id already produces, and
    `stack_summary_truncated` in the log below is the signal that a real library
    is hitting it.

    A stack-row lookup failure propagates rather than degrading the page; the
    `resolve_asset_stack_summaries` wrapper catches it and owns that posture.
    (Cover-read failures degrade per stack inside `resolve_stack_cover` and
    never reach here.)
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

    # Walked in `stack_ids` order — the order the stacks appear on the page —
    # rather than `rows.values()`, whose order comes from the backend's
    # `list_stacks` response and isn't promised to match the ids requested. That
    # only matters once the budget below truncates: spending it in page order
    # means the badges that survive are the ones on the assets nearest the top,
    # instead of an arbitrary subset.
    #
    # Fewer than two live members is not a burst a client can act on, so no
    # summary is emitted. Zero is the same "not representable" state `/stacks`
    # drops a stack for (`_build_representable_response`): its
    # `StackResponseDto` would carry an empty `assets`, and the summary would
    # hand the client a stack ID whose `GET /api/stacks/{id}` 404s.
    #
    # One is the case this sweep newly made reachable, and it needs the same
    # treatment for a different reason. Trashing one frame of a two-frame auto
    # burst is ordinary user action and leaves `asset_count` at 1 — but Immich
    # renders the badge on `asset.stack` alone, with no count threshold, so the
    # surviving photo would carry a burst badge reading "1". Upstream never
    # emits that shape: it deletes a stack once it falls below two assets, which
    # is why nothing client-side guards against it. `/stacks` would still return
    # such a stack, and that asymmetry is safe in this direction — no asset
    # advertises the ID, so no client asks for it.
    #
    # A cheap prefilter, so a burst that has drained costs no read at all;
    # `resolve_stack_cover` re-decides the zero case from the members, which is
    # what makes that half of the rule hold when this count is stale.
    representable = [
        rows[stack_id]
        for stack_id in stack_ids
        if stack_id in rows and rows[stack_id].asset_count > 1
    ]

    # Budget spent only on the rows that actually need a read. A pinned row
    # answers from its own field, so a page of user-pinned stacks is unbounded
    # in stacks but free in reads, and charging it against the budget would
    # truncate a page that costs nothing. Tested before admitting (see
    # `docs/references/code-practices.md` on why the flag would otherwise lie
    # for a page of exactly the budget).
    resolvable: list[GumnutStackRow] = []
    truncated = 0
    cover_reads = 0
    for row in representable:
        if row.primary_asset_id is None:
            if cover_reads >= STACK_SUMMARY_COVER_READ_BUDGET:
                truncated += 1
                continue
            cover_reads += 1
        resolvable.append(row)

    if truncated:
        logger.warning(
            "asset stack summaries: %d stack(s) past the %d-cover-read budget "
            "ship without a summary",
            truncated,
            STACK_SUMMARY_COVER_READ_BUDGET,
            extra={
                "stack_summary_truncated": True,
                "stack_summary_truncated_stacks": truncated,
                "stack_summary_cover_read_budget": STACK_SUMMARY_COVER_READ_BUDGET,
            },
        )

    covers = await gather_with_concurrency(
        [resolve_stack_cover(client, row) for row in resolvable]
    )

    # `assetCount` is the row's **live** member count, which is what the field
    # means to a client: web renders it directly as the burst badge's number,
    # and it has to agree with the `assets.length` a follow-up
    # `GET /api/stacks/{id}` reports, since `StackResponseDto` carries live
    # members only (see `build_stack_response`). Trashed frames counted here
    # would show a badge of 5 on a stack whose filmstrip holds 3.
    summaries = {
        row.id: AssetStackResponseDto(
            id=safe_uuid_from_stack_id(row.id),
            primaryAssetId=cover,
            assetCount=row.asset_count,
        )
        for row, cover in zip(resolvable, covers)
        if cover is not None
    }
    logger.info(
        "asset stack summaries: %d stacked asset(s) across %d stack(s), "
        "%d cover read(s), %d summary(ies) resolved "
        "(stack_summary_truncated=%s)",
        sum(1 for asset in gumnut_assets if asset.stack_id is not None),
        len(stack_ids),
        cover_reads,
        len(summaries),
        truncated > 0,
        extra={
            "stack_summary_stacks": len(stack_ids),
            "stack_summary_cover_reads": cover_reads,
            "stack_summary_resolved": len(summaries),
            "stack_summary_dangling": len(dangling),
            # Boolean under the truncation's own name, with the count beside it
            # — the shape `stack_search_truncated` established in `stacks.py`,
            # so one query form works across both stack surfaces.
            "stack_summary_truncated": truncated > 0,
            "stack_summary_truncated_stacks": truncated,
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


# --------------------------------------------------------------------------- #
# Collapsed-timeline support
# --------------------------------------------------------------------------- #


async def fetch_stack_rows(
    client: AsyncGumnut, gumnut_stack_ids: Sequence[str]
) -> list[GumnutStackRow]:
    """Fetch stack rows by ID, chunked at the Gumnut API's bulk-ID cap.

    `ids` accepts at most `GUMNUT_API_MAX_BULK_IDS` per request (over-cap
    requests 422), so a bucket referencing more distinct stacks costs one request
    per chunk. Each chunk is walked with `async for`, so nothing here depends on
    a chunk fitting in a single page. Callers index by `row.id`, not position.
    """
    rows: list[GumnutStackRow] = []
    for chunk in batched(gumnut_stack_ids, GUMNUT_API_MAX_BULK_IDS):
        rows.extend(
            [
                row
                async for row in client.stacks.list_stacks(
                    ids=list(chunk), limit=GUMNUT_API_MAX_PAGE_SIZE
                )
            ]
        )
    return rows


async def fetch_live_stack_members(
    client: AsyncGumnut, gumnut_stack_id: str
) -> list[AssetResponse]:
    """Fetch one stack's live members in ascending capture order.

    The lean sibling of `fetch_stack_members`: no `ASSET_INCLUDE`, because the
    only field the caller reads is `id`, and `state="live"` because a collapsed
    timeline can only be represented by a frame the timeline actually shows.
    `order="asc"` is pinned for the same reason it is there — the server default
    is newest-first, which would make an unpinned burst's cover its last frame.
    """
    return [
        asset
        async for asset in client.assets.list(
            stack_id=gumnut_stack_id,
            state="live",
            order="asc",
            limit=GUMNUT_API_MAX_PAGE_SIZE,
        )
    ]


def select_timeline_cover(
    stack: GumnutStackRow, live_members: Sequence[AssetResponse]
) -> str | None:
    """Pick the Gumnut asset ID of the frame that represents `stack` in a
    collapsed timeline: the pinned cover when the pin is live, otherwise the
    earliest live frame.

    `live_members` must be the stack's **complete** member set in ascending
    capture order — an incomplete set can promote a later frame, or miss a live
    pin and fall back. Returns `None` for a stack with no live members, which
    has no frame to represent it.

    The rule is `resolve_effective_primary`'s, not a second copy of it; what
    the timeline changes is the *input*. See "Timeline cover vs. effective
    primary" in `docs/architecture/adapter-architecture.md` for why the two
    surfaces resolve over different member sets.

    Liveness is enforced here rather than assumed of the caller, because it is
    the one precondition whose violation is not inert: the shared rule keeps a
    trashed pin, and a cover the bucket cannot show collapses away every live
    frame the burst has. Both of the current input paths already carry live-only
    sets, so this filters nothing today — it is what keeps the next caller from
    having to know.
    """
    live_only = [member for member in live_members if member.trashed_at is None]
    primary = resolve_effective_primary(stack, live_only)
    return primary.id if primary is not None else None


@dataclass(frozen=True, slots=True)
class TimelineStacks:
    """One time bucket's collapse decisions, keyed by Gumnut stack ID.

    Built by `resolve_timeline_stacks`. A stack ID absent from `covers` is one
    the adapter could not resolve — see there for the cases. Those are
    deliberately inert: `is_collapsed_away` keeps every frame and `tuple_for`
    emits null, so an unresolvable stack degrades to the pre-stack timeline
    rather than hiding photos.
    """

    covers: Mapping[str, str]
    """Gumnut stack ID → Gumnut asset ID of the frame representing the stack."""

    tuples: Mapping[str, list[str]]
    """Gumnut stack ID → Immich's `[stackId, assetCount]`, both strings.

    Upstream emits `array[stacked."stackId"::text, count('stacked')::text]` and
    the client parses element 1 with `Number.parseInt`, so the count is a string
    on the wire even though it is a number everywhere else.
    """

    @classmethod
    def empty(cls) -> "TimelineStacks":
        """The inert resolution: collapses nothing, badges nothing.

        Every whole-bucket degradation returns this, so "the failure paths
        degrade identically" is one object rather than a repeated literal.
        """
        return cls(covers={}, tuples={})

    def is_collapsed_away(self, asset: AssetResponse) -> bool:
        """Whether `asset` is a non-cover member and must not reach the bucket."""
        stack_id = asset.stack_id
        if stack_id is None:
            return False
        cover_id = self.covers.get(stack_id)
        return cover_id is not None and cover_id != asset.id

    def tuple_for(self, asset: AssetResponse) -> list[str] | None:
        """The `[stackId, assetCount]` tuple for `asset`, or None if it is loose."""
        stack_id = asset.stack_id
        if stack_id is None:
            return None
        return self.tuples.get(stack_id)


def _bucket_members_by_stack(
    assets: Iterable[AssetResponse],
) -> dict[str, list[AssetResponse]]:
    """Group a bucket's **live** stacked assets by stack ID, earliest first.

    Trashed assets are skipped because the caller compares `len()` of these
    groups against `row.asset_count`, which is a **live** count: counting a
    trashed frame toward it would let a partial stack classify as complete and
    resolve its cover from an incomplete set, skipping the member read that
    would have corrected it. `select_timeline_cover` re-enforces liveness on its
    own, so this is not that guard duplicated — the two protect different steps.

    Sorted on the UTC-normalized capture time so the comparison is total:
    `local_datetime` can arrive naive or aware, and mixing the two raises. `id`
    breaks exact ties so the cover is deterministic.
    """
    grouped: dict[str, list[AssetResponse]] = {}
    for asset in assets:
        if asset.stack_id is not None and asset.trashed_at is None:
            grouped.setdefault(asset.stack_id, []).append(asset)
    for members in grouped.values():
        members.sort(key=lambda a: (to_actual_utc(resolve_capture_datetime(a)), a.id))
    return grouped


async def _live_members_or_error(
    client: AsyncGumnut, gumnut_stack_id: str
) -> list[AssetResponse] | GumnutError:
    """`fetch_live_stack_members`, degrading to the error on an upstream failure.

    Caught per stack rather than propagated because the caller is the timeline's
    hottest endpoint: the assets have already been read successfully, and one
    failed member read should cost that stack its collapse, not the whole month.
    An unresolved stack is inert — see `TimelineStacks`.

    Returned rather than swallowed so the caller's aggregate record can name the
    cause; see that record for why it has to.
    """
    try:
        return await fetch_live_stack_members(client, gumnut_stack_id)
    except GumnutError as exc:
        return exc


async def resolve_timeline_stacks(
    client: AsyncGumnut, assets: Sequence[AssetResponse]
) -> TimelineStacks:
    """Resolve the collapse decisions for one bucket's assets.

    Costs one `list_stacks` request per `GUMNUT_API_MAX_BULK_IDS` distinct
    stacks, plus — only where it cannot be avoided — one lean member read per
    stack, bounded by `MAX_TIMELINE_STACK_MEMBER_READS`.

    The read is avoidable most of the time. When the bucket already holds
    `row.asset_count` live members of a stack, it holds that stack's *complete*
    live member set, so `select_timeline_cover` resolves from assets already in
    hand. A whole burst lands in one month, so the common case is zero extra
    requests.

    Two shapes reach the fallback, and they are not equivalent:

    - **A burst straddling a month boundary.** The resolved cover surfaces in
      the adjacent bucket of the same view, so the burst still renders exactly
      one tile overall.
    - **An album/person-filtered bucket that also asked for `withStacked`.** The
      cover is resolved library-wide and may fall outside the filter, in which
      case the burst is absent from that view entirely. Upstream behaves the
      same way, and no Immich client sends `withStacked` with `albumId` or
      `personId` — but any other client may, which is also why the read count is
      capped: a person-filtered month can leave hundreds of stacks partial.

    A stack the adapter cannot resolve is left out of the result rather than
    guessed at, so its frames stay in the bucket uncollapsed. The `continue`
    branches below are the cases; `docs/architecture/adapter-architecture.md`
    enumerates them alongside the whole-resource failure the route handles.
    """
    stack_ids = list(
        dict.fromkeys(asset.stack_id for asset in assets if asset.stack_id is not None)
    )
    if not stack_ids:
        return TimelineStacks.empty()

    rows_by_id = {row.id: row for row in await fetch_stack_rows(client, stack_ids)}

    # The asset read and the stack read are separate round-trips, so a stack
    # dissolved in between leaves its former members carrying a stale
    # `stack_id`. Aggregated per `docs/references/code-practices.md`.
    missing_ids = [stack_id for stack_id in stack_ids if stack_id not in rows_by_id]
    if missing_ids:
        logger.warning(
            "%d of %d timeline stacks have no stack row; leaving their frames "
            "uncollapsed (sample: %s)",
            len(missing_ids),
            len(stack_ids),
            missing_ids[:10],
            extra={
                "missing_stack_count": len(missing_ids),
                "requested_stack_count": len(stack_ids),
                "sample_stack_ids": missing_ids[:10],
            },
        )

    bucket_members = _bucket_members_by_stack(assets)

    # Classified by walking `stack_ids`, not `rows_by_id`: rows come back in the
    # Gumnut API's own order, so slicing the cap off a list built from them
    # would resolve an arbitrary subset — and, if that order is ever unstable,
    # a *different* subset per request, making tiles appear and disappear across
    # reloads. `stack_ids` is bucket order, so the cap keeps the frames nearest
    # the top of the month.
    complete_ids: list[str] = []
    partial_ids: list[str] = []
    for stack_id in stack_ids:
        row = rows_by_id.get(stack_id)
        if row is None:
            continue
        if len(bucket_members.get(stack_id, [])) == row.asset_count:
            complete_ids.append(stack_id)
        else:
            partial_ids.append(stack_id)

    read_ids = partial_ids[:MAX_TIMELINE_STACK_MEMBER_READS]
    if len(partial_ids) > len(read_ids):
        # The sample is the stacks past the cap, not the front of the list: the
        # ones that went uncollapsed are what an operator needs to look at.
        dropped_ids = partial_ids[len(read_ids) :]
        logger.warning(
            "Timeline bucket needs %d stack member reads, above the %d cap; "
            "leaving %d stacks uncollapsed (sample: %s)",
            len(partial_ids),
            MAX_TIMELINE_STACK_MEMBER_READS,
            len(dropped_ids),
            dropped_ids[:10],
            extra={
                # Named for the cause, like its three siblings, rather than for
                # the shared effect: all four leave stacks uncollapsed, so an
                # `uncollapsed_stack_count` here would read as the total while
                # reporting only this one.
                "over_cap_stack_count": len(dropped_ids),
                "partial_stack_count": len(partial_ids),
                "member_read_cap": MAX_TIMELINE_STACK_MEMBER_READS,
                "sample_stack_ids": dropped_ids[:10],
            },
        )

    fetched_members = await gather_with_concurrency(
        [_live_members_or_error(client, stack_id) for stack_id in read_ids]
    )
    live_members: dict[str, Sequence[AssetResponse]] = {
        # `.get` because a row with `asset_count == 0` classifies as complete
        # while contributing no live members to the bucket at all.
        stack_id: bucket_members.get(stack_id, [])
        for stack_id in complete_ids
    }
    # `strict=True` names the invariant this mapping rests on: results come back
    # in input order, so a length mismatch would silently pair one stack's
    # members with another's row — and a cover that is not a member collapses
    # away every frame the stack really has.
    failed_ids: list[str] = []
    first_error: GumnutError | None = None
    for stack_id, members in zip(read_ids, fetched_members, strict=True):
        if isinstance(members, GumnutError):
            failed_ids.append(stack_id)
            if first_error is None:
                first_error = members
        else:
            live_members[stack_id] = members

    if failed_ids:
        # Aggregated, and this one has the most to flood with: a degraded assets
        # resource that leaves `list_stacks` healthy never trips the route-level
        # guard, so a per-stack record would emit up to
        # `MAX_TIMELINE_STACK_MEMBER_READS` of them per request. One traceback
        # rather than none: these reads fail for the same reason far more often
        # than not, and the count alone cannot separate an expired token from
        # throttling from an outage.
        logger.warning(
            "%d of %d timeline stack member reads failed; leaving those frames "
            "uncollapsed (sample: %s)",
            len(failed_ids),
            len(read_ids),
            failed_ids[:10],
            exc_info=first_error,
            extra={
                "failed_stack_count": len(failed_ids),
                "attempted_stack_count": len(read_ids),
                "sample_stack_ids": failed_ids[:10],
                "failed_error_type": type(first_error).__name__,
                "failed_status_code": getattr(first_error, "status_code", None),
            },
        )

    covers: dict[str, str] = {}
    tuples: dict[str, list[str]] = {}
    undecodable_ids: list[str] = []
    for stack_id in stack_ids:
        row = rows_by_id.get(stack_id)
        members = live_members.get(stack_id)
        if row is None or members is None:
            # Unresolved: no row, past the read cap, or its member read failed.
            continue
        cover_id = select_timeline_cover(row, members)
        if cover_id is None:
            # No live frame to stand in for the stack. Leaving it out of
            # `covers` keeps its assets in the bucket; in a live bucket there
            # are none to keep, so this is a no-op that avoids collapsing
            # against a cover that does not exist. Deliberately silent: nothing
            # was hidden and nothing is wrong upstream.
            continue
        try:
            stack_uuid = safe_uuid_from_stack_id(stack_id)
        except ValueError:
            # The timeline is the first production consumer of the
            # `asset_stack_` prefix contract, so a backend prefix change would
            # otherwise turn a feature designed to degrade into a 500 on the
            # app's primary view. Unresolved is the honest answer here too.
            undecodable_ids.append(stack_id)
            continue
        covers[stack_id] = cover_id
        tuples[stack_id] = [str(stack_uuid), str(row.asset_count)]

    if undecodable_ids:
        # Aggregated, and a prefix change is systemic by construction, so every
        # stack in the month fails at once — and unlike the member reads,
        # nothing caps how many stacks reach this loop.
        logger.warning(
            "%d of %d timeline stack IDs are not decodable to Immich UUIDs; "
            "leaving their frames uncollapsed (sample: %s)",
            len(undecodable_ids),
            len(stack_ids),
            undecodable_ids[:10],
            extra={
                "undecodable_stack_count": len(undecodable_ids),
                "requested_stack_count": len(stack_ids),
                "sample_stack_ids": undecodable_ids[:10],
            },
        )

    return TimelineStacks(covers=covers, tuples=tuples)
