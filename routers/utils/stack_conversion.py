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

Hydration is sized for the surfaces that must return the member assets
themselves. `hydrate_stack` pulls every member with `ASSET_INCLUDE`; the
timeline's `[stackId, assetCount]` tuples need only an ID, a count, and a cover,
so they go through the lean `resolve_timeline_stacks` path further down instead.
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
from routers.immich_models import StackResponseDto, UserResponseDto
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

# Cap on the per-request fallback member reads a collapsed timeline bucket will
# issue (see `resolve_timeline_stacks`). `gather_with_concurrency` bounds
# in-flight calls but not the total, and a filtered bucket can leave every stack
# in the month partial, so without a cap one inbound request could fan out into
# hundreds. Stacks past the cap stay uncollapsed rather than uncounted — the
# same inert degradation as a missing row. Sized well above the handful of
# month-straddling bursts the main timeline actually produces.
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

    `live_members` must be the stack's **complete** live member set in ascending
    capture order, since both rules read from it — an incomplete set can promote
    a later frame, or miss a live pin and fall back. Returns `None` for a stack
    with no live members, which has no frame to represent it.

    Deliberately not `resolve_effective_primary`, which keeps a *trashed* pin so
    `StackResponseDto.primaryAssetId` can stay non-null: collapse drops every
    frame that is not the cover, so naming a trashed asset would erase the whole
    burst from the grid. See "Timeline cover vs. effective primary" in
    `docs/architecture/adapter-architecture.md` for why the two surfaces are
    allowed to disagree about a trashed pin.
    """
    if not live_members:
        return None

    pinned_id = stack.primary_asset_id
    if pinned_id is not None:
        for member in live_members:
            if member.id == pinned_id:
                return pinned_id

    return live_members[0].id


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

    Trashed assets are skipped so the result is a live member set by
    construction, whatever query produced `assets`. `select_timeline_cover`
    requires that, and today it holds only because the bucket read inherits the
    Gumnut API's `state="live"` default — an invariant that would invert
    silently if that read ever passed `state="all"`, naming a trashed cover and
    collapsing away every live frame of the burst.

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


async def _live_members_or_none(
    client: AsyncGumnut, gumnut_stack_id: str
) -> list[AssetResponse] | None:
    """`fetch_live_stack_members`, degrading to `None` on an upstream failure.

    Caught per stack rather than propagated because the caller is the timeline's
    hottest endpoint: the assets have already been read successfully, and one
    failed member read should cost that stack its collapse, not the whole month.
    An unresolved stack is inert — see `TimelineStacks`.

    Silent on purpose; the caller logs one aggregate record for the batch, for
    the same reason it aggregates the missing-row case.
    """
    try:
        return await fetch_live_stack_members(client, gumnut_stack_id)
    except GumnutError:
        return None


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

    A stack the adapter cannot resolve — no row in the response, a failed member
    read, past the read cap, or no live members — is left out of the result
    rather than guessed at, so its frames stay in the bucket uncollapsed.
    """
    stack_ids = list(
        dict.fromkeys(asset.stack_id for asset in assets if asset.stack_id is not None)
    )
    if not stack_ids:
        return TimelineStacks(covers={}, tuples={})

    rows_by_id = {row.id: row for row in await fetch_stack_rows(client, stack_ids)}

    # The asset read and the stack read are separate round-trips, so a stack
    # dissolved in between leaves its former members carrying a stale
    # `stack_id`. One aggregate record rather than one per stack: this endpoint
    # is hit once per month scrolled, and a systemic cause (an `ids` filter the
    # backend stops honoring, a library-scoping change) would otherwise flood
    # the log with one line per stack in the month. The ratio is what
    # distinguishes that from the rare single-stack race.
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
        logger.warning(
            "Timeline bucket needs %d stack member reads, above the %d cap; "
            "leaving %d stacks uncollapsed",
            len(partial_ids),
            MAX_TIMELINE_STACK_MEMBER_READS,
            len(partial_ids) - len(read_ids),
            extra={
                "partial_stack_count": len(partial_ids),
                "member_read_cap": MAX_TIMELINE_STACK_MEMBER_READS,
            },
        )

    fetched_members = await gather_with_concurrency(
        [_live_members_or_none(client, stack_id) for stack_id in read_ids]
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
    for stack_id, members in zip(read_ids, fetched_members, strict=True):
        if members is None:
            failed_ids.append(stack_id)
        else:
            live_members[stack_id] = members

    if failed_ids:
        # Aggregated for the same reason as the missing-row record above, and
        # more urgently: a degraded assets resource that leaves `list_stacks`
        # healthy never trips the route-level guard, so a per-stack record here
        # would emit up to `MAX_TIMELINE_STACK_MEMBER_READS` of them per request.
        logger.warning(
            "%d of %d timeline stack member reads failed; leaving those frames "
            "uncollapsed (sample: %s)",
            len(failed_ids),
            len(read_ids),
            failed_ids[:10],
            extra={
                "failed_stack_count": len(failed_ids),
                "attempted_stack_count": len(read_ids),
                "sample_stack_ids": failed_ids[:10],
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
        # Aggregated like the two records above, and this one needs it most: a
        # prefix change is systemic by construction, so every stack in the month
        # fails at once — and unlike the member reads, nothing caps how many
        # stacks reach this loop.
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
