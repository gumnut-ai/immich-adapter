"""Batch entity fetching from the Gumnut API."""

import logging

from gumnut import Gumnut

from routers.api.sync.types import EntityType

logger = logging.getLogger(__name__)

# Batch size for entity fetch API calls (conservative to avoid upstream limits)
FETCH_BATCH_SIZE = 100


def _batched(items: list[str], size: int) -> list[list[str]]:
    """Split a list into chunks of the given size."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def fetch_entities_map(
    gumnut_client: Gumnut,
    gumnut_entity_type: str,
    entity_ids: list[str],
) -> tuple[dict[str, EntityType], set[str]]:
    """
    Batch-fetch entities by ID and return a dict keyed by entity ID.

    IDs are chunked into batches of FETCH_BATCH_SIZE to avoid exceeding
    upstream API limits. Missing entities (deleted between event and fetch)
    result in fewer entries.

    Args:
        gumnut_client: The Gumnut API client
        gumnut_entity_type: The entity type string (e.g., "asset", "album")
        entity_ids: List of entity IDs to fetch

    Returns:
        Tuple of (entity_id -> entity object mapping, set of IDs that were
        explicitly missing — e.g., assets fetched but lacking exif data)
    """
    if not entity_ids:
        return {}, set()

    unique_ids = list(dict.fromkeys(entity_ids))  # Deduplicate, preserve order
    result: dict[str, EntityType] = {}
    missing_ids: set[str] = set()

    for chunk in _batched(unique_ids, FETCH_BATCH_SIZE):
        if gumnut_entity_type == "asset":
            page = gumnut_client.assets.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page.data})

        elif gumnut_entity_type == "album":
            page = gumnut_client.albums.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page.data})

        elif gumnut_entity_type == "person":
            page = gumnut_client.people.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page.data})

        elif gumnut_entity_type == "face":
            page = gumnut_client.faces.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page.data})

        elif gumnut_entity_type == "album_asset":
            page = gumnut_client.album_assets.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page.data})

        elif gumnut_entity_type == "exif":
            # Exif is 1:1 with asset; exif events use entity_id = asset_id
            page = gumnut_client.assets.list(ids=chunk, limit=len(chunk))
            for asset in page.data:
                if asset.exif:
                    result[asset.exif.asset_id] = asset.exif
                else:
                    logger.warning(
                        "Missing exif on fetched asset while processing exif events",
                        extra={"asset_id": asset.id},
                    )
                    missing_ids.add(asset.id)

        else:
            logger.warning(
                "Unknown entity type in fetch_entities_map",
                extra={"gumnut_entity_type": gumnut_entity_type},
            )
            return {}, set()

    return result, missing_ids
