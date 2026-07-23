from datetime import datetime, timezone
from itertools import batched
from typing import List, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from gumnut import AsyncGumnut
from gumnut.types.asset_bulk_update_assets_params import Update, UpdateChange

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS
from routers.immich_models import (
    BulkIdErrorReason,
    BulkIdResponseDto,
    BulkIdsDto,
    TagBulkAssetsDto,
    TagBulkAssetsResponseDto,
    TagCreateDto,
    TagResponseDto,
    TagUpdateDto,
    TagUpsertDto,
)
from routers.utils.asset_conversion import ASSET_INCLUDE_METADATA_ONLY
from routers.utils.current_user import get_current_user_id
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id
from services.tag_store import deterministic_tag_id, lookup_tag_value, remember_tag

router = APIRouter(
    prefix="/api/tags",
    tags=["tags"],
    responses={404: {"description": "Not found"}},
)


# The Gumnut API has no tag concept. immich-go's tagged import (`upload
# from-folder --tag <name>`) upserts the tag via `PUT /api/tags`, reads the
# returned tag id, then assigns the imported assets via
# `PUT /api/tags/{id}/assets`. The adapter emulates tags by appending the tag to
# each asset's description: upsert mints a deterministic id and records
# `id -> value` (see services/tag_store.py), and assignment recovers the value
# and appends it. The remaining tag endpoints stay informational stubs — they
# are not on immich-go's import path, and the client-side tags UI is
# deliberately left disabled (see routers/api/users.py preferences and
# routers/api/server.py server features) because `GET /api/tags` is still a stub.


def _append_tag_to_description(description: str | None, tag_value: str) -> str:
    """Append ``tag_value`` to a description as its own ``#``-prefixed line.

    Idempotent: if the exact tag line is already present the description is
    returned unchanged, so re-imports don't duplicate the tag. A tag value can
    contain spaces and ``/`` (hierarchical paths) but never a newline, so
    line-equality is an exact, collision-free membership test.
    """
    tag_line = f"#{tag_value}"
    existing = description or ""
    if tag_line in existing.split("\n"):
        return existing
    if existing:
        return f"{existing}\n{tag_line}"
    return tag_line


@router.get("")
async def get_all_tags() -> List[TagResponseDto]:
    """
    Get all tags.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("", status_code=201)
async def create_tag(request: TagCreateDto) -> TagResponseDto:
    """
    Create a tag.
    This is a stub implementation that returns a fake tag response.
    """
    return TagResponseDto(
        id=uuid4(),
        name=request.name,
        value=request.name.lower().replace(" ", "-"),
        color=request.color,
        parentId=str(request.parentId) if request.parentId else None,
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.put("")
async def upsert_tags(
    request: TagUpsertDto,
    current_user_id: UUID = Depends(get_current_user_id),
) -> List[TagResponseDto]:
    """Upsert tags (create or resolve each requested name).

    The Gumnut API has no tags, so the adapter emulates them (see the module
    comment). Each requested name is mapped to a deterministic synthetic id —
    idempotent across repeated upserts and identical across workers — and the
    ``id -> value`` mapping is recorded in Redis so a later
    ``PUT /api/tags/{id}/assets`` can recover the value and append it to each
    asset's description.

    Returns one ``TagResponseDto`` per requested name, in the same order:
    immich-go reads the upserted tag back positionally, and matches on the
    echoed ``value``, so names are echoed verbatim (``value`` = full path,
    ``name`` = leaf segment).
    """
    user_id = str(current_user_id)
    now = datetime.now(tz=timezone.utc)
    responses: List[TagResponseDto] = []
    for value in request.tags:
        tag_id = deterministic_tag_id(user_id, value)
        await remember_tag(user_id, tag_id, value)
        responses.append(
            TagResponseDto(
                id=tag_id,
                name=value.rsplit("/", 1)[-1],
                value=value,
                color=None,
                parentId=None,
                createdAt=now,
                updatedAt=now,
            )
        )
    return responses


@router.get("/{id}")
async def get_tag_by_id(id: UUID) -> TagResponseDto:
    """
    Get tag by ID.
    This is a stub implementation that returns a fake tag response.
    """
    return TagResponseDto(
        id=id,
        name="Sample Tag",
        value="sample-tag",
        color="#ff0000",
        parentId=None,
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.put("/{id}")
async def update_tag(id: UUID, request: TagUpdateDto) -> TagResponseDto:
    """
    Update tag.
    This is a stub implementation that returns a fake tag response.
    """
    return TagResponseDto(
        id=id,
        name="Updated Tag",
        value="updated-tag",
        color=request.color,
        parentId=None,
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.delete("/{id}", status_code=204)
async def delete_tag(id: UUID):
    """
    Delete tag.
    This is a stub implementation that does not perform any action.
    """
    return


@router.put("/{id}/assets")
async def tag_assets(
    id: UUID,
    request: BulkIdsDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user_id: UUID = Depends(get_current_user_id),
) -> List[BulkIdResponseDto]:
    """Assign assets to a tag by appending the tag to each asset's description.

    The Gumnut API has no tags, so "assignment" means appending the tag value —
    recovered from the id minted at upsert time — as a line to each asset's
    description. Reads the requested assets' current descriptions
    (``state="all"`` so trashed assets aren't silently dropped), appends the tag
    idempotently, and writes changed descriptions back via
    ``bulk_update_assets``. Calls are chunked by ``GUMNUT_API_MAX_BULK_IDS``.

    Returns one ``BulkIdResponseDto`` per requested id: ``success=True`` for
    assets that were updated (or already carried the tag), and
    ``success=False`` / ``not_found`` for ids the user's scoped read didn't
    return (inaccessible or nonexistent). An unknown tag id — one never recorded
    at upsert time — is a ``400``, matching Immich's tag-not-found behavior.
    """
    user_id = str(current_user_id)
    tag_value = await lookup_tag_value(user_id, id)
    if tag_value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tag not found",
        )

    if not request.ids:
        return []

    accessible: set[UUID] = set()
    for chunk in batched(request.ids, GUMNUT_API_MAX_BULK_IDS):
        gumnut_ids = [uuid_to_gumnut_asset_id(uid) for uid in chunk]
        page = await client.assets.list(
            state="all",
            ids=gumnut_ids,
            limit=len(gumnut_ids),
            include=ASSET_INCLUDE_METADATA_ONLY,
        )
        current_desc_by_id: dict[str, str | None] = {
            asset.id: (asset.metadata.description if asset.metadata else None)
            for asset in page.data
        }

        updates: list[Update] = []
        for uid, gid in zip(chunk, gumnut_ids):
            if gid not in current_desc_by_id:
                continue  # inaccessible / nonexistent → not_found below
            accessible.add(uid)
            existing = current_desc_by_id[gid]
            new_desc = _append_tag_to_description(existing, tag_value)
            if new_desc != (existing or ""):
                updates.append(
                    {
                        "id": gid,
                        "change": cast(UpdateChange, {"description": new_desc}),
                    }
                )

        if updates:
            await client.assets.bulk_update_assets(updates=updates)

    return [
        BulkIdResponseDto(
            id=uid,
            success=uid in accessible,
            error=None if uid in accessible else BulkIdErrorReason.not_found,
            errorMessage=None if uid in accessible else "Asset not found",
        )
        for uid in request.ids
    ]


@router.delete("/{id}/assets")
async def untag_assets(id: UUID, request: BulkIdsDto) -> List[BulkIdResponseDto]:
    """
    Bulk remove assets from tag.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.put("/assets")
async def bulk_tag_assets(request: TagBulkAssetsDto) -> TagBulkAssetsResponseDto:
    """
    Tag assets.
    This is a stub implementation that returns an empty list.
    """
    return TagBulkAssetsResponseDto(count=0)
