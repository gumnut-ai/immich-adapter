"""Batch entity fetching from the Gumnut API."""

import logging
from uuid import UUID

from gumnut import AsyncGumnut
from gumnut.types.album_asset_response import AlbumAssetResponse
from gumnut.types.album_response import AlbumResponse
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
    """A stack row was returned but no member could be read for it."""


async def _first_stack_member(
    client: AsyncGumnut, stack_id: str, *, asset_id: str | None = None
) -> AssetResponse | None:
    """Return the first member in any state without walking later pages."""
    if asset_id is None:
        page = client.assets.list(stack_id=stack_id, state="all", order="asc", limit=1)
    else:
        page = client.assets.list(
            stack_id=stack_id,
            ids=[asset_id],
            state="all",
            order="asc",
            limit=1,
        )
    return await anext(aiter(page), None)


async def _resolve_stack_primary_for_sync(
    client: AsyncGumnut, stack_row: StackListStacksResponse
) -> UUID | None:
    """Resolve a stack primary, skipping only an undecodable stack ID.

    The row's live member IDs settle every stack with a live member and no
    trashed pin. Only the rest cost a member read.
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

    live_ids = stack_row.asset_ids
    if live_ids is None:
        raise StackMemberReadInconsistent(
            f"stack {stack_row.id} was listed without its member ids"
        )

    pin = stack_row.primary_asset_id
    primary_id = pin if pin in live_ids else None
    if primary_id is None and pin is not None:
        # A trashed pin is still the cover, and only a member read can see it.
        pinned = await _first_stack_member(client, stack_row.id, asset_id=pin)
        primary_id = pinned.id if pinned is not None else None
    if primary_id is None and live_ids:
        primary_id = live_ids[0]
    if primary_id is None:
        trashed = await _first_stack_member(client, stack_row.id)
        primary_id = trashed.id if trashed is not None else None
    if primary_id is None:
        # Propagate so the cursor is preserved: the row exists, so an empty
        # all-state read is not known to be permanent.
        raise StackMemberReadInconsistent(
            f"stack {stack_row.id} member read returned none"
        )
    return safe_uuid_from_asset_id(primary_id)


async def _read_library_assets(
    client: AsyncGumnut, asset_ids: list[str]
) -> dict[str, AssetResponse]:
    """Read which of ``asset_ids`` the client's library holds, in any state.

    An omitted ID is absent from the library, so every page is read before an
    ID counts as omitted and a failed read propagates.

    ``state="all"`` keeps trashed assets, so a trash event hydrates and an
    album cover pointing at a trashed asset survives until restore.
    """
    page = await client.assets.list(
        state="all",
        ids=asset_ids,
        limit=len(asset_ids),
        include=ASSET_INCLUDE_NO_PEOPLE,
    )
    found = {asset.id: asset for asset in page.data}
    # A full page reports more to come; stop once every ID is accounted for.
    while len(found) < len(asset_ids) and page.has_next_page():
        page = await page.get_next_page()
        found.update({asset.id: asset for asset in page.data})
    return found


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
            result.update(await _read_library_assets(gumnut_client, chunk))

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
            # asset_ids keeps primary resolution off the per-stack read path,
            # which exhausts the upstream rate limit on a first sync.
            stack_page = await gumnut_client.stacks.list_stacks(
                ids=chunk, limit=len(chunk), include=["asset_ids"]
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


async def fetch_current_album_memberships(
    gumnut_client: AsyncGumnut, pairs: set[tuple[str, str]]
) -> dict[tuple[str, str], AlbumAssetResponse]:
    """Read which ``(album_id, asset_id)`` pairs are album members now.

    A removal event names a pair, not a row, and a later transaction can re-add
    the pair yet sort ahead of the removal in the events feed. Pairs absent from
    the result are not members.

    Each read is a rate-limited list call. One removal request removes many
    assets from one album, so an album with several pairs is read whole when
    that takes fewer pages than one call per pair. A deleted album has no
    members and costs nothing further.
    """
    by_album: dict[str, set[str]] = {}
    for album_id, asset_id in pairs:
        by_album.setdefault(album_id, set()).add(asset_id)
    shared = [album_id for album_id, assets in by_album.items() if len(assets) > 1]
    albums, _ = await fetch_entities_map(gumnut_client, "album", shared)

    async def _read_pair(album_id: str, asset_id: str) -> list[AlbumAssetResponse]:
        page = await gumnut_client.album_assets.list(
            album_id=album_id, asset_id=asset_id, limit=1
        )
        return list(page.data)

    async def _read_album(album_id: str) -> list[AlbumAssetResponse]:
        wanted = by_album[album_id]
        return [
            row
            async for row in gumnut_client.album_assets.list(
                album_id=album_id, limit=GUMNUT_API_MAX_BULK_IDS
            )
            if row.asset_id in wanted
        ]

    reads = []
    for album_id, asset_ids in sorted(by_album.items()):
        if len(asset_ids) == 1:
            reads.append(_read_pair(album_id, next(iter(asset_ids))))
            continue
        album = albums.get(album_id)
        if not isinstance(album, AlbumResponse):
            continue
        pages = -(-album.asset_count // GUMNUT_API_MAX_BULK_IDS)
        if pages < len(asset_ids):
            reads.append(_read_album(album_id))
        else:
            reads.extend(_read_pair(album_id, a) for a in sorted(asset_ids))

    rows = await gather_with_concurrency(reads, cancel_on_error=True)
    return {(row.album_id, row.asset_id): row for found in rows for row in found}
