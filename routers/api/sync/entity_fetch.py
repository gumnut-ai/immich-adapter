"""Batch entity fetching from the Gumnut API."""

import logging

from gumnut import AsyncGumnut
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


async def _hydrate_stack_for_sync(
    client: AsyncGumnut, stack_row: StackListStacksResponse
) -> HydratedStack | None:
    """Hydrate one stack, skipping only *permanent* non-emittable conditions.

    Returns `None` — routed to the inert `missing_ids` skip — only when the stack
    can never be emitted and skipping strands no member: an undecodable id
    (prefix drift; its members degrade to loose via `_immich_stack_id`), or a
    member-less stack (`hydrate_stack` returns `None`; no asset carries its
    `stackId`). The decode is guarded around the call so a member
    `ValidationError` — also a `ValueError` — raised deeper in hydration is not
    swallowed with it.

    A transient `GumnutError` from the member read is deliberately **not** caught.
    It is retriable, and skipping it would let `_stream_entity_type` advance the
    events cursor past the stack while the asset pass still stamps `stackId` on
    its members — permanently hiding the whole burst, since the mobile timeline
    drops an asset whose `stackId` names no stack row. Propagating truncates this
    sync so the cursor is preserved and the stack retries next cycle. A stack
    that fails *persistently* wedges the pass until it recovers — loud in the
    logs, and the price of never stranding a member; bounded per-stack retry is a
    possible future refinement.
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
    return await hydrate_stack(client, stack_row)


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
            # blocking round-trip per stack. A permanently non-emittable stack
            # (member-less, or undecodable id per _hydrate_stack_for_sync) yields
            # None and goes to missing_ids — the same inert skip as an asset
            # lacking metadata; a transient member-read failure propagates
            # instead (see _hydrate_stack_for_sync for why skipping would hide
            # the burst).
            stack_page = await gumnut_client.stacks.list_stacks(
                ids=chunk, limit=len(chunk)
            )
            rows = stack_page.data
            hydrated_stacks = await gather_with_concurrency(
                [_hydrate_stack_for_sync(gumnut_client, row) for row in rows]
            )
            for stack_row, hydrated in zip(rows, hydrated_stacks, strict=True):
                if hydrated is None:
                    missing_ids.add(stack_row.id)
                else:
                    result[stack_row.id] = FetchedStack(
                        row=stack_row, primary_asset_id=hydrated.primary_asset_id
                    )

    return result, missing_ids
