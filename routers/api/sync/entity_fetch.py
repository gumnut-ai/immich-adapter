"""Batch entity fetching from the Gumnut API."""

import logging

from gumnut import AsyncGumnut

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS
from routers.api.sync.types import EntityType, FetchedStack
from routers.utils.asset_conversion import ASSET_INCLUDE_NO_PEOPLE
from routers.utils.stack_conversion import hydrate_stack

logger = logging.getLogger(__name__)


def _batched(items: list[str], size: int) -> list[list[str]]:
    """Split a list into chunks of the given size."""
    return [items[i : i + size] for i in range(0, len(items), size)]


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
            # Stacks need a hop the other types don't: SyncStackV1 requires a
            # primaryAssetId, and for an unpinned burst that cover is only known
            # by reading the members. hydrate_stack resolves it (reusing the REST
            # effective-primary rules) and the resolved id rides along in
            # FetchedStack so convert_entity_to_sync_event stays I/O-free. A
            # member-less stack has no honest primary, so it goes to missing_ids
            # and is skipped — the same inert path as an asset lacking metadata.
            stack_page = await gumnut_client.stacks.list_stacks(
                ids=chunk, limit=len(chunk)
            )
            for stack_row in stack_page.data:
                hydrated = await hydrate_stack(gumnut_client, stack_row)
                if hydrated is None:
                    missing_ids.add(stack_row.id)
                    continue
                result[stack_row.id] = FetchedStack(
                    row=stack_row, primary_asset_id=hydrated.primary_asset_id
                )

    return result, missing_ids
