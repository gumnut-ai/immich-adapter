"""
Events processing, entity fetching, and sync stream generation.

Imports converter functions from converters module.
"""

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, TypeAlias, cast

from gumnut import Gumnut
from gumnut.types.album_asset_response import AlbumAssetResponse
from gumnut.types.album_response import AlbumResponse
from gumnut.types.asset_response import AssetResponse
from gumnut.types.events_response import Data as EventData
from gumnut.types.exif_response import ExifResponse
from gumnut.types.face_response import FaceResponse
from gumnut.types.person_response import PersonResponse

from routers.immich_models import (
    SyncAlbumDeleteV1,
    SyncAlbumToAssetDeleteV1,
    SyncAssetDeleteV1,
    SyncAssetFaceDeleteV1,
    SyncEntityType,
    SyncPersonDeleteV1,
    SyncRequestType,
    SyncStreamDto,
)
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_album_id,
    safe_uuid_from_asset_id,
    safe_uuid_from_face_id,
    safe_uuid_from_person_id,
    safe_uuid_from_user_id,
)
from services.checkpoint_store import Checkpoint

from routers.api.sync.converters import (
    gumnut_album_asset_to_sync_album_to_asset_v1,
    gumnut_album_to_sync_album_v1,
    gumnut_asset_to_sync_asset_v1,
    gumnut_exif_to_sync_exif_v1,
    gumnut_face_to_sync_face_v1,
    gumnut_person_to_sync_person_v1,
    gumnut_user_to_sync_auth_user_v1,
    gumnut_user_to_sync_user_v1,
)

logger = logging.getLogger(__name__)

# Page size for events API pagination
EVENTS_PAGE_SIZE = 500

# Batch size for entity fetch API calls (conservative to avoid upstream limits)
FETCH_BATCH_SIZE = 100

# Delete event types that are converted to Immich delete sync models
_DELETE_EVENT_TYPES = frozenset(
    {
        "asset_deleted",
        "album_deleted",
        "person_deleted",
        "face_deleted",
        "album_asset_removed",
    }
)

# Event types that are intentionally skipped (not converted to sync events)
_SKIPPED_EVENT_TYPES = frozenset(
    {
        "exif_deleted",  # Immich handles via asset deletion
    }
)

# Expected entity_type for each delete event_type
_DELETE_EVENT_ENTITY_TYPES: dict[str, str] = {
    "asset_deleted": "asset",
    "album_deleted": "album",
    "person_deleted": "person",
    "face_deleted": "face",
    "album_asset_removed": "album_asset",
}

# Mapping from SyncRequestType to (gumnut_entity_type, SyncEntityType)
# Order matters - assets before exif, albums before album_assets, etc.
# This ordering ensures FK parents are streamed before children during upserts.
_SYNC_TYPE_ORDER: list[tuple[SyncRequestType, str, SyncEntityType]] = [
    (SyncRequestType.AssetsV1, "asset", SyncEntityType.AssetV1),
    (SyncRequestType.AlbumsV1, "album", SyncEntityType.AlbumV1),
    (SyncRequestType.AlbumToAssetsV1, "album_asset", SyncEntityType.AlbumToAssetV1),
    (SyncRequestType.AssetExifsV1, "exif", SyncEntityType.AssetExifV1),
    (SyncRequestType.PeopleV1, "person", SyncEntityType.PersonV1),
    (SyncRequestType.AssetFacesV1, "face", SyncEntityType.AssetFaceV1),
]

# Order for streaming delete events — reverse of FK dependency order.
# Children are deleted before parents so the client can clean up FK references
# before the referenced entity is removed. This is the inverse of
# _SYNC_TYPE_ORDER's upsert ordering.
_DELETE_TYPE_ORDER: list[SyncEntityType] = [
    SyncEntityType.AssetFaceDeleteV1,
    SyncEntityType.AlbumToAssetDeleteV1,
    SyncEntityType.PersonDeleteV1,
    SyncEntityType.AlbumDeleteV1,
    SyncEntityType.AssetDeleteV1,
]

_EntityType: TypeAlias = (
    AssetResponse
    | AlbumResponse
    | AlbumAssetResponse
    | PersonResponse
    | FaceResponse
    | ExifResponse
)

# Map gumnut entity type -> SyncEntityType (derived from _SYNC_TYPE_ORDER at module load)
_GUMNUT_TYPE_TO_SYNC_TYPE: dict[str, SyncEntityType] = {
    gumnut_type: sync_type for _, gumnut_type, sync_type in _SYNC_TYPE_ORDER
}

# Supported SyncRequestTypes (used to detect unsupported types requested by client)
_SUPPORTED_REQUEST_TYPES: frozenset[SyncRequestType] = frozenset(
    {request_type for request_type, _, _ in _SYNC_TYPE_ORDER}
    | {SyncRequestType.AuthUsersV1, SyncRequestType.UsersV1}
)

# FK references: gumnut_entity_type -> [(attribute_name, referenced_gumnut_entity_type)]
_FK_REFERENCES: dict[str, list[tuple[str, str]]] = {
    "face": [("person_id", "person"), ("asset_id", "asset")],
    "album_asset": [("album_id", "album"), ("asset_id", "asset")],
    "album": [("album_cover_asset_id", "asset")],
}


@dataclass
class SyncStreamStats:
    """Tracks streamed entity IDs and skip counts during sync stream generation."""

    streamed_ids: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    entity_not_found_skips: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    delete_event_skips: int = 0
    buffered_deletes: int = 0
    fk_warnings: int = 0


def _check_fk_references(
    gumnut_entity_type: str,
    entity: _EntityType,
    stats: SyncStreamStats,
    checkpoint_map: dict[SyncEntityType, Checkpoint],
    cursor: str,
) -> None:
    """Warn if entity references IDs not seen in this sync for fully-synced entity types.

    Only warns when the referenced entity type has no checkpoint (i.e., it was
    fully synced in this cycle), since a prior checkpoint means the referenced
    entity may have been synced in an earlier cycle.
    """
    refs = _FK_REFERENCES.get(gumnut_entity_type)
    if not refs:
        return

    for attr_name, ref_type in refs:
        ref_id = getattr(entity, attr_name, None)
        if ref_id is None:
            continue

        # If the referenced entity type has a checkpoint, skip the check —
        # the referenced entity may have been synced in a prior cycle
        ref_sync_type = _GUMNUT_TYPE_TO_SYNC_TYPE.get(ref_type)
        if ref_sync_type and ref_sync_type in checkpoint_map:
            continue

        if ref_id not in stats.streamed_ids.get(ref_type, set()):
            logger.warning(
                "Entity references ID not seen in this sync",
                extra={
                    "entity_type": gumnut_entity_type,
                    "entity_id": getattr(entity, "id", None),
                    "reference_field": attr_name,
                    "referenced_type": ref_type,
                    "referenced_id": ref_id,
                    "cursor": cursor,
                },
            )
            stats.fk_warnings += 1


def _to_ack_string(
    entity_type: SyncEntityType,
    cursor: str,
) -> str:
    """
    Convert entity type and cursor to ack string.

    Ack format for immich-adapter: "SyncEntityType|cursor|"

    The cursor MUST NOT contain pipe characters — ``_parse_ack()`` splits on
    ``|`` and would silently truncate the cursor, corrupting the checkpoint.
    Upstream cursors are opaque strings controlled by photos-api; if they
    ever include pipes this assertion will surface the issue immediately.

    Args:
        entity_type: The sync entity type
        cursor: The opaque events cursor (must not contain '|')

    Returns:
        Formatted ack string
    """
    if "|" in cursor:
        logger.error(
            "Cursor contains pipe character, ack format will be corrupted",
            extra={"entity_type": entity_type.value, "cursor": cursor},
        )
    return f"{entity_type.value}|{cursor}|"


def _make_sync_event(
    entity_type: SyncEntityType,
    data: dict,
    cursor: str,
) -> str:
    """
    Create a sync event JSON line.

    Args:
        entity_type: The Immich sync entity type
        data: The entity data dict
        cursor: The opaque events cursor for checkpointing

    Returns:
        JSON line string with newline
    """
    ack = _to_ack_string(entity_type, cursor)

    return (
        json.dumps(
            {
                "type": entity_type.value,
                "data": data,
                "ack": ack,
            }
        )
        + "\n"
    )


def _make_delete_sync_event(
    event: EventData,
) -> tuple[str, SyncEntityType] | None:
    """
    Convert a delete event to an Immich delete sync event JSON line.

    Validates that event.entity_type matches expectations for the delete
    event_type. Logs a warning and skips if there's a mismatch.

    Args:
        event: The event with a delete event_type

    Returns:
        Tuple of (json_line, sync_entity_type) or None if event should be skipped
    """
    # Validate entity_type matches the delete event_type
    expected_entity_type = _DELETE_EVENT_ENTITY_TYPES.get(event.event_type)
    if expected_entity_type and event.entity_type != expected_entity_type:
        logger.warning(
            "Delete event entity_type mismatch, skipping",
            extra={
                "event_type": event.event_type,
                "entity_type": event.entity_type,
                "expected_entity_type": expected_entity_type,
                "entity_id": event.entity_id,
                "cursor": event.cursor,
            },
        )
        return None

    if event.event_type == "asset_deleted":
        data = SyncAssetDeleteV1(assetId=str(safe_uuid_from_asset_id(event.entity_id)))
        return (
            _make_sync_event(
                SyncEntityType.AssetDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.AssetDeleteV1,
        )

    elif event.event_type == "album_deleted":
        data = SyncAlbumDeleteV1(albumId=str(safe_uuid_from_album_id(event.entity_id)))
        return (
            _make_sync_event(
                SyncEntityType.AlbumDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.AlbumDeleteV1,
        )

    elif event.event_type == "person_deleted":
        data = SyncPersonDeleteV1(
            personId=str(safe_uuid_from_person_id(event.entity_id))
        )
        return (
            _make_sync_event(
                SyncEntityType.PersonDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.PersonDeleteV1,
        )

    elif event.event_type == "face_deleted":
        data = SyncAssetFaceDeleteV1(
            assetFaceId=str(safe_uuid_from_face_id(event.entity_id))
        )
        return (
            _make_sync_event(
                SyncEntityType.AssetFaceDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.AssetFaceDeleteV1,
        )

    elif event.event_type == "album_asset_removed":
        if not isinstance(event.payload, dict):
            logger.warning(
                "album_asset_removed event payload missing or invalid, skipping",
                extra={
                    "event_type": event.event_type,
                    "cursor": event.cursor,
                    "created_at": event.created_at,
                    "entity_id": event.entity_id,
                    "payload": event.payload,
                },
            )
            return None

        album_id = event.payload.get("album_id")
        asset_id = event.payload.get("asset_id")
        if not isinstance(album_id, (str, int)) or not isinstance(asset_id, (str, int)):
            logger.warning(
                "album_asset_removed event album_id/asset_id missing or invalid type, skipping",
                extra={
                    "event_type": event.event_type,
                    "cursor": event.cursor,
                    "created_at": event.created_at,
                    "entity_id": event.entity_id,
                    "payload": event.payload,
                },
            )
            return None

        album_id_str = str(album_id).strip()
        asset_id_str = str(asset_id).strip()
        if not album_id_str or not asset_id_str:
            logger.warning(
                "album_asset_removed event album_id/asset_id empty after conversion, skipping",
                extra={
                    "event_type": event.event_type,
                    "cursor": event.cursor,
                    "created_at": event.created_at,
                    "entity_id": event.entity_id,
                    "payload": event.payload,
                },
            )
            return None

        data = SyncAlbumToAssetDeleteV1(
            albumId=str(safe_uuid_from_album_id(album_id_str)),
            assetId=str(safe_uuid_from_asset_id(asset_id_str)),
        )
        return (
            _make_sync_event(
                SyncEntityType.AlbumToAssetDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.AlbumToAssetDeleteV1,
        )

    logger.warning(
        "Unhandled delete event type in _make_delete_sync_event",
        extra={
            "event_type": event.event_type,
            "entity_id": event.entity_id,
            "cursor": event.cursor,
        },
    )
    return None


def _convert_entity_to_sync_event(
    gumnut_entity_type: str,
    entity: _EntityType,
    owner_id: str,
    cursor: str,
    sync_entity_type: SyncEntityType,
) -> str:
    """
    Convert a fetched entity to an Immich sync event JSON line.

    Args:
        gumnut_entity_type: The entity type string (e.g., "asset", "album")
        entity: The fetched entity object
        owner_id: UUID of the owner
        cursor: The event cursor for the ack string
        sync_entity_type: The Immich sync entity type

    Returns:
        JSON line string with newline
    """
    if gumnut_entity_type == "asset":
        sync_model = gumnut_asset_to_sync_asset_v1(
            cast(AssetResponse, entity), owner_id
        )
    elif gumnut_entity_type == "album":
        sync_model = gumnut_album_to_sync_album_v1(
            cast(AlbumResponse, entity), owner_id
        )
    elif gumnut_entity_type == "person":
        sync_model = gumnut_person_to_sync_person_v1(
            cast(PersonResponse, entity), owner_id
        )
    elif gumnut_entity_type == "album_asset":
        sync_model = gumnut_album_asset_to_sync_album_to_asset_v1(
            cast(AlbumAssetResponse, entity)
        )
    elif gumnut_entity_type == "face":
        sync_model = gumnut_face_to_sync_face_v1(cast(FaceResponse, entity))
    elif gumnut_entity_type == "exif":
        sync_model = gumnut_exif_to_sync_exif_v1(cast(ExifResponse, entity))
    else:
        raise ValueError(f"Unsupported entity type: {gumnut_entity_type}")

    return _make_sync_event(
        sync_entity_type,
        sync_model.model_dump(mode="json"),
        cursor,
    )


def _batched(items: list[str], size: int) -> list[list[str]]:
    """Split a list into chunks of the given size."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _fetch_entities_map(
    gumnut_client: Gumnut,
    gumnut_entity_type: str,
    entity_ids: list[str],
) -> tuple[dict[str, _EntityType], set[str]]:
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
    result: dict[str, _EntityType] = {}
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
                "Unknown entity type in _fetch_entities_map",
                extra={"gumnut_entity_type": gumnut_entity_type},
            )
            return {}, set()

    return result, missing_ids


async def _stream_entity_type(
    gumnut_client: Gumnut,
    gumnut_entity_type: str,
    sync_entity_type: SyncEntityType,
    owner_id: str,
    checkpoint: Checkpoint | None,
    sync_started_at: datetime,
    stats: SyncStreamStats,
    checkpoint_map: dict[SyncEntityType, Checkpoint],
    delete_buffer: list[tuple[str, SyncEntityType]] | None = None,
) -> AsyncGenerator[tuple[str, int], None]:
    """
    Stream events for a single entity type using the events API.

    Fetches lightweight events, then batch-fetches full entities for
    upsert events. Delete events are either yielded directly or buffered
    for later delivery depending on the ``delete_buffer`` parameter.

    When ``delete_buffer`` is provided, delete events are appended to it
    instead of being yielded. This supports the two-phase streaming
    strategy where all upserts are streamed first (preserving FK parent
    ordering) and deletes are streamed afterward in reverse FK order.

    Args:
        gumnut_client: The Gumnut API client
        gumnut_entity_type: The entity type string for the Gumnut API (e.g., "asset")
        sync_entity_type: The Immich sync entity type (e.g., SyncEntityType.AssetV1)
        owner_id: The owner UUID string
        checkpoint: The checkpoint with cursor (None for full sync)
        sync_started_at: Upper bound for the query window
        stats: Mutable stats tracker for streamed IDs, skip counts, and FK warnings
        checkpoint_map: All checkpoints for this sync (used for FK reference checks)
        delete_buffer: If provided, delete events are appended here instead of yielded

    Yields:
        Tuples of (json_line, count) for each upsert event (deletes buffered when delete_buffer is set)
    """
    last_cursor = checkpoint.cursor if checkpoint else None
    count = 0

    while True:
        # Build params for events API.
        # created_at_lt bounds the query to a point-in-time snapshot so events
        # created during streaming are deferred to the next sync cycle.  This
        # is required because cursor ordering alone doesn't prevent tailing new
        # events indefinitely — the time bound guarantees the stream terminates.
        params: dict[str, Any] = {
            "created_at_lt": sync_started_at,
            "entity_types": gumnut_entity_type,
            "limit": EVENTS_PAGE_SIZE,
        }
        if last_cursor is not None:
            params["after_cursor"] = last_cursor

        events_response = gumnut_client.events.get(**params)

        events = events_response.data
        if not events:
            break

        # Collect entity IDs from upsert events (non-delete, non-skipped)
        upsert_ids = [
            event.entity_id
            for event in events
            if event.event_type not in _DELETE_EVENT_TYPES
            and event.event_type not in _SKIPPED_EVENT_TYPES
        ]

        # Batch-fetch entities for upserts
        entities_map, missing_ids = _fetch_entities_map(
            gumnut_client, gumnut_entity_type, upsert_ids
        )

        # Process events in order
        for event in events:
            if event.event_type in _SKIPPED_EVENT_TYPES:
                # Intentionally skipped event type — advance cursor only
                logger.debug(
                    "Skipping unsupported event type",
                    extra={
                        "event_type": event.event_type,
                        "entity_id": event.entity_id,
                        "entity_type": event.entity_type,
                    },
                )
                continue

            if event.event_type in _DELETE_EVENT_TYPES:
                # Delete event — convert and either buffer or yield
                result = _make_delete_sync_event(event)
                if result:
                    json_line, delete_sync_type = result
                    if delete_buffer is not None:
                        delete_buffer.append((json_line, delete_sync_type))
                        stats.buffered_deletes += 1
                    else:
                        yield json_line, 1
                        count += 1
                else:
                    stats.delete_event_skips += 1
            else:
                # Upsert event — look up fetched entity
                entity = entities_map.get(event.entity_id)
                if entity is None:
                    # Entity was deleted between event and fetch, or
                    # explicitly missing (e.g., asset fetched but no exif).
                    # For exif events, event.entity_id == asset_id, which
                    # matches the asset.id stored in missing_ids by
                    # _fetch_entities_map when an asset lacks exif data.
                    if event.entity_id in missing_ids:
                        logger.warning(
                            "Entity explicitly missing from fetch result",
                            extra={
                                "entity_type": gumnut_entity_type,
                                "entity_id": event.entity_id,
                                "event_type": event.event_type,
                                "cursor": event.cursor,
                            },
                        )
                    else:
                        logger.warning(
                            "Entity not found during sync, likely deleted between "
                            "event fetch and entity fetch",
                            extra={
                                "entity_type": gumnut_entity_type,
                                "entity_id": event.entity_id,
                                "event_type": event.event_type,
                                "cursor": event.cursor,
                            },
                        )
                    stats.entity_not_found_skips[gumnut_entity_type] += 1
                    continue

                # face_created events should not carry person_id.
                # Face detection always creates faces without a person.
                # The current entity state may include a person_id assigned
                # later by clustering, but the corresponding person_created
                # event may not be in this sync cycle. Null it out — the
                # face_updated event from clustering will deliver the correct
                # person_id in the same or a future sync cycle.
                if (
                    sync_entity_type == SyncEntityType.AssetFaceV1
                    and event.event_type == "face_created"
                    and isinstance(entity, FaceResponse)
                    and entity.person_id is not None
                ):
                    entity = entity.model_copy(update={"person_id": None})

                # face_updated events carry the causally-consistent
                # person_id in the event payload. Use it instead of the
                # entity's current state, which may reference a person
                # assigned by a later clustering run.
                elif (
                    sync_entity_type == SyncEntityType.AssetFaceV1
                    and event.event_type == "face_updated"
                    and isinstance(entity, FaceResponse)
                    and isinstance(event.payload, dict)
                    and "person_id" in event.payload
                ):
                    payload_person_id = event.payload["person_id"]
                    if payload_person_id is None or (
                        isinstance(payload_person_id, str) and payload_person_id.strip()
                    ):
                        if entity.person_id != payload_person_id:
                            entity = entity.model_copy(
                                update={"person_id": payload_person_id}
                            )

                # Track streamed entity ID before FK check so the current
                # entity is visible to its own reference validation
                entity_id = getattr(entity, "id", None)
                if entity_id is not None:
                    stats.streamed_ids[gumnut_entity_type].add(entity_id)

                # Check FK references before yielding
                _check_fk_references(
                    gumnut_entity_type, entity, stats, checkpoint_map, event.cursor
                )

                json_line = _convert_entity_to_sync_event(
                    gumnut_entity_type, entity, owner_id, event.cursor, sync_entity_type
                )
                yield json_line, 1
                count += 1

        # Update cursor from last event
        last_cursor = events[-1].cursor

        if not events_response.has_more:
            break

    if count > 0:
        logger.debug(
            f"Streamed {count} {sync_entity_type.value} events",
            extra={"entity_type": sync_entity_type.value, "count": count},
        )


def _yield_buffered_deletes(
    delete_buffer: list[tuple[str, SyncEntityType]],
) -> list[tuple[str, SyncEntityType]]:
    """
    Sort buffered delete events in reverse FK dependency order.

    Groups deletes by SyncEntityType and returns them in ``_DELETE_TYPE_ORDER``
    (children before parents), preserving chronological order within each type.
    Any delete types not in ``_DELETE_TYPE_ORDER`` are appended at the end.

    Args:
        delete_buffer: List of (json_line, SyncEntityType) tuples

    Returns:
        Sorted list of (json_line, SyncEntityType) tuples
    """
    if not delete_buffer:
        return []

    # Group by SyncEntityType, preserving insertion (chronological) order
    groups: dict[SyncEntityType, list[tuple[str, SyncEntityType]]] = defaultdict(list)
    for item in delete_buffer:
        groups[item[1]].append(item)

    result: list[tuple[str, SyncEntityType]] = []

    # Yield in _DELETE_TYPE_ORDER (reverse FK dependency)
    for delete_type in _DELETE_TYPE_ORDER:
        if delete_type in groups:
            result.extend(groups.pop(delete_type))

    # Defensive: yield any remaining types not in _DELETE_TYPE_ORDER
    for remaining in groups.values():
        result.extend(remaining)

    return result


async def generate_sync_stream(
    gumnut_client: Gumnut,
    request: SyncStreamDto,
    checkpoint_map: dict[SyncEntityType, Checkpoint],
) -> AsyncGenerator[str, None]:
    """
    Generate sync stream as JSON Lines (newline-delimited JSON).

    Uses the photos-api events endpoint to fetch lightweight event records
    in priority order, then batch-fetches full entities for upsert events.

    The stream is split into two phases to maintain FK integrity:
    - Phase 1: All upserts in FK dependency order (parents before children)
    - Phase 2: All deletes in reverse FK order (children before parents)

    This prevents FK constraint violations in the mobile client, which
    batches events by type and enforces FK constraints at insert time.

    Each entity type uses its own checkpoint with an opaque cursor for
    cursor-based pagination.

    Each line is a JSON object with: type, data, and ack (checkpoint cursor).
    """
    try:
        # Get current user for owner_id
        current_user = gumnut_client.users.me()
        owner_id = str(safe_uuid_from_user_id(current_user.id))

        requested_types = set(request.types)

        # Log unsupported sync types requested by the client
        unsupported_types = requested_types - _SUPPORTED_REQUEST_TYPES
        if unsupported_types:
            logger.info(
                "Client requested unsupported sync types",
                extra={
                    "user_id": owner_id,
                    "unsupported_types": sorted(t.value for t in unsupported_types),
                },
            )

        logger.info(
            f"Starting sync stream with {len(requested_types)} entity types",
            extra={
                "user_id": owner_id,
                "types": [t.value for t in requested_types],
                "reset": request.reset,
                "checkpoints": len(checkpoint_map),
            },
        )

        stats = SyncStreamStats()

        # User/auth-user entities don't go through events API.
        # Use updated_at as the cursor for delta semantics: re-stream
        # when the user record has changed since the last ack.
        user_cursor = (
            current_user.updated_at.isoformat()
            if current_user.updated_at
            else current_user.id
        )

        # Stream auth user if requested
        if SyncRequestType.AuthUsersV1 in requested_types:
            checkpoint = checkpoint_map.get(SyncEntityType.AuthUserV1)
            if checkpoint is None or checkpoint.cursor != user_cursor:
                sync_auth_user = gumnut_user_to_sync_auth_user_v1(current_user)
                yield _make_sync_event(
                    SyncEntityType.AuthUserV1,
                    sync_auth_user.model_dump(mode="json"),
                    user_cursor,
                )
                logger.debug("Streamed auth user", extra={"user_id": owner_id})

        # Stream user if requested
        if SyncRequestType.UsersV1 in requested_types:
            checkpoint = checkpoint_map.get(SyncEntityType.UserV1)
            if checkpoint is None or checkpoint.cursor != user_cursor:
                sync_user = gumnut_user_to_sync_user_v1(current_user)
                yield _make_sync_event(
                    SyncEntityType.UserV1,
                    sync_user.model_dump(mode="json"),
                    user_cursor,
                )
                logger.debug("Streamed user", extra={"user_id": owner_id})

        # Capture sync start time to bound the query window
        sync_started_at = datetime.now(timezone.utc)

        # Counters for logging
        event_counts: dict[str, int] = {}
        total_events = 0

        # Phase 1: Stream upserts for all entity types in FK dependency order,
        # buffering delete events for phase 2.
        delete_buffer: list[tuple[str, SyncEntityType]] = []

        for request_type, gumnut_entity_type, sync_entity_type in _SYNC_TYPE_ORDER:
            if request_type not in requested_types:
                continue

            # Get checkpoint for this entity type
            checkpoint = checkpoint_map.get(sync_entity_type)

            # Stream upsert events, buffer deletes
            async for event_line, count in _stream_entity_type(
                gumnut_client,
                gumnut_entity_type,
                sync_entity_type,
                owner_id,
                checkpoint,
                sync_started_at,
                stats,
                checkpoint_map,
                delete_buffer=delete_buffer,
            ):
                yield event_line
                event_counts[sync_entity_type.value] = (
                    event_counts.get(sync_entity_type.value, 0) + count
                )
                total_events += count

        # Phase 2: Yield buffered deletes in reverse FK dependency order
        # (children before parents) so the client can clean up FK references
        # before the referenced parent entity is removed.
        for json_line, delete_entity_type in _yield_buffered_deletes(delete_buffer):
            yield json_line
            event_counts[delete_entity_type.value] = (
                event_counts.get(delete_entity_type.value, 0) + 1
            )
            total_events += 1

        # Log summary with skip counts
        summary_extra: dict[str, Any] = {
            "user_id": owner_id,
            "total_events": total_events,
            "event_counts": event_counts,
        }
        if stats.entity_not_found_skips:
            summary_extra["entity_not_found_skips"] = dict(stats.entity_not_found_skips)
        if stats.delete_event_skips > 0:
            summary_extra["delete_event_skips"] = stats.delete_event_skips
        if stats.buffered_deletes > 0:
            summary_extra["buffered_deletes"] = stats.buffered_deletes
        if stats.fk_warnings > 0:
            summary_extra["fk_reference_warnings"] = stats.fk_warnings

        logger.info("Sync stream summary", extra=summary_extra)

        # Stream completion event
        yield _make_sync_event(SyncEntityType.SyncCompleteV1, {}, "complete")
        logger.info("Sync stream completed", extra={"user_id": owner_id})

    except Exception:
        logger.error("Error generating sync stream", exc_info=True)


async def _generate_reset_stream() -> AsyncGenerator[str, None]:
    """
    Generate a sync stream containing only SyncResetV1.

    Used when the session has isPendingSyncReset flag set.
    Matches immich behavior: send SyncResetV1 and end immediately.
    """
    yield (
        json.dumps(
            {
                "type": SyncEntityType.SyncResetV1.value,
                "data": {},
                "ack": _to_ack_string(SyncEntityType.SyncResetV1, "reset"),
            }
        )
        + "\n"
    )
