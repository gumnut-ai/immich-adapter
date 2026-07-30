import logging
from typing import Annotated, List
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from gumnut import AsyncGumnut, NotFoundError
from pydantic.json_schema import SkipJsonSchema

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.immich_models import (
    StackResponseDto,
    StackCreateDto,
    StackUpdateDto,
    BulkIdsDto,
    UserResponseDto,
)
from routers.utils.current_user import get_current_user
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_stack_id,
)
from routers.utils.stack_conversion import (
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
# with the whole library or truncate — and untruncated it is the one request
# here whose cost has no ceiling. Every stack costs its own member walk, so N
# stacks means N upstream reads of full `ASSET_INCLUDE` payloads, all resident
# at once; `hydrate_stacks` bounds how many run concurrently but not how many
# run. This cap is what makes that finite: 500 member walks at the shared
# fan-out bound is 50 sequential waves, and the response still carries every
# member of all 500.
#
# `list_stacks` orders by stack ID — stable but arbitrary — so a truncated
# answer drops a consistent but meaningless subset rather than "the oldest".
# A library that routinely trips this wants the transpose `hydrate_stacks`
# describes (one asset walk grouped by `stack_id`) instead of a bigger cap; the
# log below is what tells you it is happening.
SEARCH_STACKS_CAP = 500


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
        return []

    if hydrated is None or hydrated.primary_asset_id != primary_asset_id:
        return []
    return [build_stack_response(hydrated, current_user)]


@router.get("")
async def search_stacks(
    primaryAssetId: Annotated[UUID | SkipJsonSchema[None], Query()] = None,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> List[StackResponseDto]:
    """List the caller's stacks, optionally narrowed to one by its cover asset.

    Immich's `searchStacks` takes no pagination parameters, so the walk below
    exhausts the Gumnut API's cursor rather than answering with its first page,
    bounded only by `SEARCH_STACKS_CAP`.

    Stacks come back in the Gumnut API's own order (by stack ID: stable, but
    neither chronological nor otherwise meaningful). Upstream imposes no order
    either.

    Member-less stacks are dropped rather than represented — `hydrate_stack`
    logs each one. An upstream failure on any single stack fails the whole
    request, which `hydrate_stacks` leaves to callers to choose: a partial list
    that silently omits stacks a backend hiccup touched is worse here than a
    loud failure, since the client can't tell the two apart.
    """
    if primaryAssetId is not None:
        return await _search_by_primary_asset(client, primaryAssetId, current_user)

    stacks = []
    async for stack in client.stacks.list_stacks(limit=GUMNUT_API_MAX_PAGE_SIZE):
        stacks.append(stack)
        if len(stacks) >= SEARCH_STACKS_CAP:
            break

    hydrated = await hydrate_stacks(client, stacks)
    responses = [
        build_stack_response(stack, current_user)
        for stack in hydrated
        if stack is not None
    ]
    logger.info(
        "stack search: walked %d stacks, returned %d (cap_hit=%s)",
        len(stacks),
        len(responses),
        len(stacks) >= SEARCH_STACKS_CAP,
        extra={
            "stacks_walked": len(stacks),
            "stacks_returned": len(responses),
            "stack_cap_hit": len(stacks) >= SEARCH_STACKS_CAP,
        },
    )
    return responses


@router.delete("", status_code=204)
async def delete_stacks(request: BulkIdsDto):
    """
    Delete multiple stacks.
    This is a stub implementation that does not perform any action.
    """
    return


@router.post("", status_code=201)
async def create_stack(request: StackCreateDto) -> StackResponseDto:
    """
    Create a stack.
    This is a stub implementation that returns a fake stack response.
    """
    return StackResponseDto(id=uuid4(), primaryAssetId=request.assetIds[0], assets=[])


@router.get("/{id}")
async def get_stack(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> StackResponseDto:
    """Return one stack with its live members, cover first.

    A stack that doesn't exist — or belongs to another user, which the Gumnut
    API answers identically — surfaces as a 404 through the global `GumnutError`
    handler. A member-less row 404s here instead, since `hydrate_stack` can't
    render one.
    """
    stack = await client.stacks.retrieve_stack(uuid_to_gumnut_stack_id(id))
    hydrated = await hydrate_stack(client, stack)
    if hydrated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Stack not found"
        )
    return build_stack_response(hydrated, current_user)


@router.put("/{id}")
async def update_stack(id: UUID, request: StackUpdateDto) -> StackResponseDto:
    """
    Update stack.
    This is a stub implementation that returns a fake stack response.
    """
    return StackResponseDto(
        id=id,
        primaryAssetId=request.primaryAssetId if request.primaryAssetId else uuid4(),
        assets=[],
    )


@router.delete("/{id}", status_code=204)
async def delete_stack(id: UUID):
    """
    Delete stack.
    This is a stub implementation that does not perform any action.
    """
    return


@router.delete("/{id}/assets/{assetId}", status_code=204)
async def remove_asset_from_stack(id: UUID, assetId: UUID):
    """
    Remove asset from stack.
    This is a stub implementation that does not perform any action.
    """
    return
