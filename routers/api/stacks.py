import logging
from typing import Annotated, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from gumnut import AsyncGumnut, GumnutError, NotFoundError
from pydantic.json_schema import SkipJsonSchema

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.immich_models import (
    StackResponseDto,
    StackCreateDto,
    StackUpdateDto,
    BulkIdsDto,
    UserResponseDto,
)
from routers.utils.concurrency import gather_with_concurrency
from routers.utils.current_user import get_current_user
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_stack_id,
)
from routers.utils.stack_conversion import (
    HydratedStack,
    build_stack_response,
    hydrate_stack,
    hydrate_stacks,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/stacks",
    tags=["stacks"],
    responses={404: {"description": "Not found"}},
)

# Hard cap on stacks returned by an unfiltered `searchStacks`. Immich gives the
# client no way to ask for a second page, so the endpoint must either answer
# with the whole library or truncate. A library that routinely trips this wants
# the transpose `hydrate_stacks` describes rather than a bigger number here —
# the truncation log below is what surfaces that.
SEARCH_STACKS_CAP = 500

# Companion bound on the *members* the walk commits to hydrating. The stack cap
# alone bounds rows, not work: 500 stacks of 3 frames and 500 stacks of 10,000
# each satisfy it, while costing two wildly different numbers of upstream pages
# and two wildly different peak footprints. Budgeting the members up front is
# possible because a listing row carries its own `asset_count` — the size is
# known before anything is fetched.
#
# 500 stacks × ~10 frames is a generous ceiling for the bursts this endpoint
# actually serves, so a library that trips this is already past the shape the
# read is built for.
SEARCH_STACKS_MEMBER_BUDGET = 5000


def _build_representable_response(
    hydrated: HydratedStack | None, current_user: UserResponseDto
) -> StackResponseDto | None:
    """Build an Immich DTO, or `None` when the stack has no live members."""
    if hydrated is None:
        return None
    response = build_stack_response(hydrated, current_user)
    if not response.assets:
        return None
    return response


async def _search_by_primary_asset(
    client: AsyncGumnut, primary_asset_id: UUID, current_user: UserResponseDto
) -> List[StackResponseDto]:
    """Return the stack whose *effective* cover is `primary_asset_id`, if any.

    The Gumnut API's own `primary_asset_id` filter on `list_stacks` is the
    obvious implementation and the wrong one: it matches only covers a user
    explicitly pinned, while the adapter synthesizes a cover for every
    auto-detected burst (see `resolve_effective_primary`). Forwarding the filter
    would answer "no stack" for exactly the bursts whose cover Immich is already
    displaying. So resolve the asset's own stack instead, and compare against
    the cover the adapter actually reports.

    A `NotFoundError` from any of the three reads yields an empty result rather
    than a 404. Upstream's search is a plain equality filter that matches
    nothing for an unknown ID; these lookups are an adapter implementation
    detail, and letting them turn a *search* into a not-found would also fail
    the whole request for a stack deleted while it was being resolved. The
    member read is inside the guard for that reason — it has the widest window
    of the three.
    """
    try:
        asset = await client.assets.retrieve(uuid_to_gumnut_asset_id(primary_asset_id))
        if asset.stack_id is None:
            return []
        stack = await client.stacks.retrieve_stack(asset.stack_id)
        hydrated = await hydrate_stack(client, stack)
    except NotFoundError:
        # The only branch here that reaches the client as an ordinary "no
        # match" while actually being an upstream 404 — every other upstream
        # failure propagates. Logged so a genuine fault on the asset or stack
        # read is distinguishable from the races this guard exists for.
        logger.info(
            "Stack search for primary asset %s hit an upstream 404; no match",
            primary_asset_id,
            extra={"primary_asset_id": str(primary_asset_id)},
        )
        return []

    if hydrated is None or hydrated.primary_asset_id != primary_asset_id:
        return []
    response = _build_representable_response(hydrated, current_user)
    return [response] if response is not None else []


@router.get("")
async def search_stacks(
    primaryAssetId: Annotated[UUID | SkipJsonSchema[None], Query()] = None,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> List[StackResponseDto]:
    """List the caller's stacks, optionally narrowed to one by its cover asset.

    Immich's `searchStacks` takes no pagination parameters, so the walk below
    exhausts the Gumnut API's cursor rather than answering with its first page,
    bounded by `SEARCH_STACKS_CAP` on rows and `SEARCH_STACKS_MEMBER_BUDGET` on
    the members those rows commit to hydrating.

    Stacks come back in the Gumnut API's own order (by stack ID: stable, but
    neither chronological nor otherwise meaningful). Upstream imposes no order
    either.

    Stacks with nothing to show are dropped — see
    `_build_representable_response`. An upstream failure on any single stack
    fails the whole request, which `hydrate_stacks` leaves to callers to choose:
    a partial list that silently omits stacks a backend hiccup touched is worse
    here than a loud failure, since the client can't tell the two apart.
    """
    if primaryAssetId is not None:
        return await _search_by_primary_asset(client, primaryAssetId, current_user)

    stacks = []
    budgeted_members = 0
    truncated_by: str | None = None
    async for stack in client.stacks.list_stacks(limit=GUMNUT_API_MAX_PAGE_SIZE):
        # Tested before admitting, so the flag below means "a row was left out",
        # not "a limit was reached" — a library of exactly SEARCH_STACKS_CAP
        # stacks exhausts the cursor and never sets it. Since the flag is the
        # only signal that a library has outgrown the endpoint, a false positive
        # on the boundary would cost it its meaning.
        if len(stacks) >= SEARCH_STACKS_CAP:
            truncated_by = "stack_cap"
            break
        if budgeted_members >= SEARCH_STACKS_MEMBER_BUDGET:
            truncated_by = "member_budget"
            break
        stacks.append(stack)
        # The row's live count, which undercounts what hydration actually
        # fetches: `fetch_stack_members` reads `state="all"`, so trashed members
        # ride along unbudgeted. Budgeting the live count keeps the bound
        # expressed in the same units as the response; the log below records the
        # realized total alongside it so the gap is visible rather than assumed.
        budgeted_members += stack.asset_count
        # Admission is all-or-nothing per stack (never a truncated member array
        # — see `build_stack_response`), so the stack that crosses the budget
        # still hydrates whole and the real bound is the budget plus one stack.
        # Warned about only when that one stack outweighs the entire budget:
        # dropping a user's largest stack would be a worse answer than a slow
        # one, so the cost is paid, but not silently. At most one such stack can
        # be admitted — the next iteration always breaks on the budget.
        if stack.asset_count > SEARCH_STACKS_MEMBER_BUDGET:
            logger.warning(
                "stack search hydrating stack %s whole: %d members exceeds the "
                "entire %d-member budget",
                stack.id,
                stack.asset_count,
                SEARCH_STACKS_MEMBER_BUDGET,
                extra={
                    "stack_id": stack.id,
                    "stack_members": stack.asset_count,
                    "stack_member_budget": SEARCH_STACKS_MEMBER_BUDGET,
                },
            )

    hydrated = await hydrate_stacks(client, stacks)
    hydrated_members = sum(
        len(stack.members) for stack in hydrated if stack is not None
    )
    responses = [
        response
        for response in (
            _build_representable_response(stack, current_user) for stack in hydrated
        )
        if response is not None
    ]
    # `stack_search_truncated` is the queryable "this library was cut short"
    # flag, deliberately not named after either bound since both set it;
    # `stack_truncated_by` says which one did, because the remedies differ —
    # more stacks than the endpoint shape supports, versus a few stacks big
    # enough to blow the member budget on their own. `stack_members_hydrated` is
    # what was actually read, against the live count that was budgeted.
    logger.info(
        "stack search: walked %d stacks (%d members budgeted, %d hydrated), "
        "returned %d (stack_search_truncated=%s, stack_truncated_by=%s)",
        len(stacks),
        budgeted_members,
        hydrated_members,
        len(responses),
        truncated_by is not None,
        truncated_by,
        extra={
            "stacks_walked": len(stacks),
            "stack_members_budgeted": budgeted_members,
            "stack_members_hydrated": hydrated_members,
            "stacks_returned": len(responses),
            "stack_search_truncated": truncated_by is not None,
            "stack_truncated_by": truncated_by,
        },
    )
    return responses


@router.delete("", status_code=204)
async def delete_stacks(
    request: BulkIdsDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """Dissolve every stack in the bulk id list; the photos are untouched.

    The SDK has no bulk-delete, so this fans out one delete per id. Ids are
    deduped (request order preserved) so a stack named twice doesn't turn its
    own dissolve into a not-found on the second call.

    Not atomic: a mid-batch failure leaves earlier deletes committed. Every
    call is allowed to settle, then the first `GumnutError` in request order is
    raised — a deterministic result — via the global handler. Non-SDK errors
    propagate immediately. An empty list is a 204 no-op.
    """
    # `dict.fromkeys` dedupes while preserving first-seen order (UUIDs hash).
    gumnut_stack_ids = [
        uuid_to_gumnut_stack_id(stack_id) for stack_id in dict.fromkeys(request.ids)
    ]
    if not gumnut_stack_ids:
        return

    async def _delete(gumnut_stack_id: str) -> GumnutError | None:
        try:
            await client.stacks.delete(gumnut_stack_id)
            return None
        except GumnutError as exc:
            return exc

    errors = await gather_with_concurrency(
        [_delete(gumnut_stack_id) for gumnut_stack_id in gumnut_stack_ids]
    )
    first_error = next((error for error in errors if error is not None), None)
    if first_error is not None:
        raise first_error
    return


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_stack(
    request: StackCreateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> StackResponseDto:
    """Group the requested assets into a new stack, covered by the first one.

    The backend owns validation and reconciliation, so the request is forwarded
    once and the response is hydrated from the returned stack. Oversized
    requests are not chunked because a partial merge cannot be rolled back.

    Unlike reads, a successfully created stack with no live members is returned
    so the client still receives its ID.
    """
    gumnut_asset_ids = [
        uuid_to_gumnut_asset_id(asset_id) for asset_id in request.assetIds
    ]
    stack = await client.stacks.create_stack(
        asset_ids=gumnut_asset_ids,
        primary_asset_id=gumnut_asset_ids[0],
    )

    hydrated = await hydrate_stack(client, stack)
    if hydrated is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stack was created but could not be read back",
        )

    response = build_stack_response(hydrated, current_user)
    if not response.assets:
        logger.warning(
            "Created stack %s has no live members; returning a stack response "
            "with an empty asset list",
            stack.id,
            extra={"stack_id": stack.id, "stack_asset_count": stack.asset_count},
        )
    return response


@router.get("/{id}")
async def get_stack(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> StackResponseDto:
    """Return one stack with its live members, cover first.

    A stack that doesn't exist — or belongs to another user, which the Gumnut
    API answers identically — surfaces as a 404 through the global `GumnutError`
    handler. A stack with nothing to show 404s here instead, on the same rule
    the list uses to drop one (see `_build_representable_response`).
    """
    stack = await client.stacks.retrieve_stack(uuid_to_gumnut_stack_id(id))
    hydrated = await hydrate_stack(client, stack)
    response = _build_representable_response(hydrated, current_user)
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Stack not found"
        )
    return response


@router.put("/{id}")
async def update_stack(
    id: UUID,
    request: StackUpdateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> StackResponseDto:
    """Set the stack cover, or return the unchanged stack when none is supplied.

    This deprecated PUT remains the route used by generated Immich clients;
    the replacement PATCH is excluded from the upstream OpenAPI spec. The
    backend validates the cover, and unrepresentable stacks return 404 as they
    do from `get_stack`.
    """
    gumnut_stack_id = uuid_to_gumnut_stack_id(id)

    if request.primaryAssetId is None:
        stack = await client.stacks.retrieve_stack(gumnut_stack_id)
    else:
        stack = await client.stacks.set_cover(
            gumnut_stack_id,
            primary_asset_id=uuid_to_gumnut_asset_id(request.primaryAssetId),
        )

    hydrated = await hydrate_stack(client, stack)
    response = _build_representable_response(hydrated, current_user)
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Stack not found"
        )
    return response


@router.delete("/{id}", status_code=204)
async def delete_stack(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """Dissolve one stack; only the grouping is removed, the photos are not.

    A missing or foreign stack 404s through the global `GumnutError` handler,
    as it does from `get_stack`.
    """
    await client.stacks.delete(uuid_to_gumnut_stack_id(id))
    return


@router.delete("/{id}/assets/{assetId}", status_code=204)
async def remove_asset_from_stack(
    id: UUID,
    assetId: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """Remove one asset from a stack, leaving the asset itself untouched.

    Cover clearing and dissolution below two members are owned by the backend's
    `remove_assets`, so the adapter just forwards the request. Removing a
    non-member is a silent upstream success; a missing or foreign stack 404s
    through the global `GumnutError` handler.
    """
    await client.stacks.remove_assets(
        uuid_to_gumnut_stack_id(id),
        asset_ids=[uuid_to_gumnut_asset_id(assetId)],
    )
    return
