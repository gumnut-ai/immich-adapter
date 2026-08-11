"""Batch entity fetching from the Gumnut API."""

import logging

from gumnut import AsyncGumnut, GumnutError
from gumnut.types.stack_list_stacks_response import StackListStacksResponse

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS
from routers.api.sync.types import EntityType, FetchedStack
from routers.utils.asset_conversion import ASSET_INCLUDE_NO_PEOPLE
from routers.utils.concurrency import gather_with_concurrency
from routers.utils.gumnut_id_conversion import safe_uuid_from_stack_id
from routers.utils.stack_conversion import HydratedStack, hydrate_stack

logger = logging.getLogger(__name__)


def _batched(items: list[str], size: int) -> list[list[str]]:
    """Split a list into chunks of the given size."""
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _hydrate_stack_or_skip(
    client: AsyncGumnut, stack_row: StackListStacksResponse
) -> HydratedStack | None:
    """Hydrate one stack, degrading a decode or member-read failure to a skip.

    StackV1 is the first pass in the sync stream, so an unhandled error here
    escapes the whole `gather` batch (losing every sibling's `StackV1` too) and
    truncates every later pass plus `SyncCompleteV1`. Two conditions are degraded
    to the inert `missing_ids` skip instead — matching how the timeline and the
    asset `stackId` converter already degrade them, rather than the delete path
    that crashes:

    - an undecodable stack id (prefix drift), guarded around the decode call
      itself so a member `ValidationError` — also a `ValueError` — raised deeper
      in hydration is not swallowed with it; and
    - a transient member-read `GumnutError` (the per-stack catch `hydrate_stacks`'
      docstring points callers to, since a batch-level failure drops siblings).
    """
    try:
        safe_uuid_from_stack_id(stack_row.id)
    except ValueError:
        logger.warning(
            "Undecodable stack id during sync; skipping the stack row and "
            "syncing its assets as loose",
            extra={"stack_id": stack_row.id},
        )
        return None
    try:
        return await hydrate_stack(client, stack_row)
    except GumnutError:
        logger.warning(
            "Stack member read failed during sync; syncing its assets as loose "
            "and skipping the stack row",
            extra={"stack_id": stack_row.id},
            exc_info=True,
        )
        return None


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
            # hydrate_stack resolves the effective primary carried in
            # FetchedStack (see its docstring for why the converter stays
            # I/O-free). Each hydration is a per-stack member read, so run them
            # under the shared concurrency bound rather than serially: a first
            # sync replays every stack event and would otherwise open one
            # blocking round-trip per stack. A member-less stack (or one whose
            # member read fails, per _hydrate_stack_or_skip) yields None and goes
            # to missing_ids — the same inert skip as an asset lacking metadata.
            stack_page = await gumnut_client.stacks.list_stacks(
                ids=chunk, limit=len(chunk)
            )
            rows = stack_page.data
            hydrated_stacks = await gather_with_concurrency(
                [_hydrate_stack_or_skip(gumnut_client, row) for row in rows]
            )
            for stack_row, hydrated in zip(rows, hydrated_stacks, strict=True):
                if hydrated is None:
                    missing_ids.add(stack_row.id)
                else:
                    result[stack_row.id] = FetchedStack(
                        row=stack_row, primary_asset_id=hydrated.primary_asset_id
                    )

    return result, missing_ids
