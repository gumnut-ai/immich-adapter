"""Batch entity fetching from the Gumnut API."""

import logging
from typing import Literal
from uuid import UUID

from gumnut import AsyncGumnut
from gumnut.types.asset_response import AssetResponse
from gumnut.types.face_response import FaceResponse
from gumnut.types.stack_list_stacks_response import StackListStacksResponse

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS
from routers.api.sync.types import EntityType, FetchedStack
from routers.utils.asset_conversion import (
    ASSET_INCLUDE_NO_PEOPLE,
    should_expose_face_geometry,
)
from routers.utils.concurrency import gather_with_concurrency
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_stack_id,
)

logger = logging.getLogger(__name__)


def _batched(items: list[str], size: int) -> list[list[str]]:
    """Split a list into chunks of the given size."""
    return [items[i : i + size] for i in range(0, len(items), size)]


class StackMemberReadInconsistent(Exception):
    """A stack row was returned but its all-state member read was empty."""


async def _first_stack_member(
    client: AsyncGumnut,
    stack_id: str,
    *,
    state: Literal["live", "all"],
    asset_id: str | None = None,
) -> AssetResponse | None:
    """Return the first matching member without walking later cursor pages."""
    if asset_id is None:
        page = client.assets.list(stack_id=stack_id, state=state, order="asc", limit=1)
    else:
        page = client.assets.list(
            stack_id=stack_id,
            ids=[asset_id],
            state=state,
            order="asc",
            limit=1,
        )
    return await anext(aiter(page), None)


async def _resolve_stack_primary_for_sync(
    client: AsyncGumnut, stack_row: StackListStacksResponse
) -> UUID | None:
    """Resolve a stack primary, skipping only an undecodable stack ID."""
    try:
        safe_uuid_from_stack_id(stack_row.id)
    except ValueError:
        logger.warning(
            "Undecodable stack id during sync; skipping the stack row and "
            "syncing its assets as loose",
            extra={"stack_id": stack_row.id},
        )
        return None

    primary = None
    if stack_row.primary_asset_id is not None:
        primary = await _first_stack_member(
            client,
            stack_row.id,
            state="all",
            asset_id=stack_row.primary_asset_id,
        )
    if primary is None:
        primary = await _first_stack_member(client, stack_row.id, state="live")
    if primary is None:
        primary = await _first_stack_member(client, stack_row.id, state="all")
    if primary is None:
        # asset_count excludes trashed members, so even zero cannot prove that
        # an empty all-state read is permanent. Propagate to preserve the cursor.
        raise StackMemberReadInconsistent(
            f"stack {stack_row.id} member read returned none "
            f"(row reports {stack_row.asset_count} live member(s))"
        )
    return safe_uuid_from_asset_id(primary.id)


async def fetch_entities_map(
    gumnut_client: AsyncGumnut,
    gumnut_entity_type: str,
    entity_ids: list[str],
) -> tuple[dict[str, EntityType], set[str]]:
    """
    Batch-fetch entities by ID and return a dict keyed by entity ID.

    IDs are chunked at ``GUMNUT_API_MAX_BULK_IDS`` to stay within the upstream
    API limit. Missing entities (deleted between event and fetch) result in
    fewer entries.

    Args:
        gumnut_client: The async Gumnut API client
        gumnut_entity_type: The entity type string (e.g., "asset", "album")
        entity_ids: List of entity IDs to fetch

    Returns:
        Tuple of (entity_id -> entity object mapping, set of IDs that were
        explicitly missing — e.g., assets fetched but lacking metadata)
    """
    _SUPPORTED_TYPES = {
        "asset",
        "album",
        "person",
        "face",
        "album_asset",
        "metadata",
        "stack",
    }
    if gumnut_entity_type not in _SUPPORTED_TYPES:
        raise ValueError(
            f"Unsupported entity type in fetch_entities_map: {gumnut_entity_type}"
        )

    if not entity_ids:
        return {}, set()

    unique_ids = list(dict.fromkeys(entity_ids))  # Deduplicate, preserve order
    result: dict[str, EntityType] = {}
    missing_ids: set[str] = set()

    for chunk in _batched(unique_ids, GUMNUT_API_MAX_BULK_IDS):
        if gumnut_entity_type == "asset":
            # state="all" includes trashed assets so ASSET_TRASHED events hydrate
            # successfully — the default live-only filter would silently drop
            # them from page.data, dropping the event before it reaches the
            # client. Also covers payload-ref FK verification: album_cover_asset_id
            # pointing at a trashed asset must not be nulled out, since restore
            # should keep the cover intact.
            page = await gumnut_client.assets.list(
                state="all",
                ids=chunk,
                limit=len(chunk),
                include=ASSET_INCLUDE_NO_PEOPLE,
            )
            result.update({entity.id: entity for entity in page.data})

        elif gumnut_entity_type == "album":
            page = await gumnut_client.albums.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page.data})

        elif gumnut_entity_type == "person":
            page = await gumnut_client.people.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page.data})

        elif gumnut_entity_type == "face":
            page = await gumnut_client.faces.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page.data})

        elif gumnut_entity_type == "album_asset":
            page = await gumnut_client.album_assets.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page.data})

        elif gumnut_entity_type == "metadata":
            # Metadata is 1:1 with asset; metadata events use entity_id = asset_id.
            # Store the full AssetResponse (not just asset.metadata) because the
            # metadata converter needs asset-level fields (width, height,
            # file_size_bytes).
            page = await gumnut_client.assets.list(
                ids=chunk, limit=len(chunk), include=ASSET_INCLUDE_NO_PEOPLE
            )
            for asset in page.data:
                if asset.metadata:
                    result[asset.id] = asset
                else:
                    logger.warning(
                        "Missing metadata on fetched asset while processing "
                        "metadata events",
                        extra={"asset_id": asset.id},
                    )
                    missing_ids.add(asset.id)

        elif gumnut_entity_type == "stack":
            # Primary resolution costs one lean member read per stack, bounded
            # here so a first sync does not fan out without limit.
            stack_page = await gumnut_client.stacks.list_stacks(
                ids=chunk, limit=len(chunk)
            )
            rows = stack_page.data
            primary_ids = await gather_with_concurrency(
                [_resolve_stack_primary_for_sync(gumnut_client, row) for row in rows],
                cancel_on_error=True,
            )
            for stack_row, primary_id in zip(rows, primary_ids, strict=True):
                if primary_id is None:
                    missing_ids.add(stack_row.id)
                else:
                    result[stack_row.id] = FetchedStack(
                        row=stack_row, primary_asset_id=primary_id
                    )

    return result, missing_ids


async def fetch_suppressed_face_ids(
    gumnut_client: AsyncGumnut,
    faces: list[FaceResponse],
) -> set[str]:
    """Return face ids whose owning assets do not expose geometry.

    Owners are deduplicated and fetched with ``state="all"``, chunked at
    ``GUMNUT_API_MAX_BULK_IDS``. Missing owners are suppressed fail-safe. No
    ``include`` is needed because the predicate reads lean-core ``kind``.
    """
    asset_ids = list(dict.fromkeys(face.asset_id for face in faces))
    if not asset_ids:
        return set()

    fetched_assets: dict[str, AssetResponse] = {}
    for chunk in _batched(asset_ids, GUMNUT_API_MAX_BULK_IDS):
        page = await gumnut_client.assets.list(state="all", ids=chunk, limit=len(chunk))
        fetched_assets.update({asset.id: asset for asset in page.data})

    unfetched = set(asset_ids) - fetched_assets.keys()
    if unfetched:
        logger.warning(
            "Owning assets missing during face-geometry gating; suppressing "
            "their face rows fail-safe",
            extra={"asset_ids": sorted(unfetched)},
        )

    exposable_asset_ids = {
        asset.id
        for asset in fetched_assets.values()
        if should_expose_face_geometry(asset)
    }
    return {face.id for face in faces if face.asset_id not in exposable_asset_ids}
