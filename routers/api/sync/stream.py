"""
V2 events processing, entity fetching, and sync stream generation.

Imports converter functions from converters module.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, TypeAlias, cast

from gumnut import Gumnut
from gumnut.types.album_asset_response import AlbumAssetResponse
from gumnut.types.album_response import AlbumResponse
from gumnut.types.asset_response import AssetResponse
from gumnut.types.events_v2_response import Data as EventV2Data
from gumnut.types.exif_response import ExifResponse
from gumnut.types.face_response import FaceResponse
from gumnut.types.person_response import PersonResponse

from routers.immich_models import (
    SyncAlbumDeleteV1,
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
    }
)

# Event types that are intentionally skipped (not converted to sync events)
_SKIPPED_EVENT_TYPES = frozenset(
    {
        "exif_deleted",  # Immich handles via asset deletion
        "album_asset_removed",  # Record is gone by deletion time; can't resolve albumId+assetId
    }
)

# Expected entity_type for each delete event_type
_DELETE_EVENT_ENTITY_TYPES: dict[str, str] = {
    "asset_deleted": "asset",
    "album_deleted": "album",
    "person_deleted": "person",
    "face_deleted": "face",
}

# Mapping from SyncRequestType to (gumnut_entity_type, SyncEntityType)
# Order matters - assets before exif, albums before album_assets, etc.
_SYNC_TYPE_ORDER: list[tuple[SyncRequestType, str, SyncEntityType]] = [
    (SyncRequestType.AssetsV1, "asset", SyncEntityType.AssetV1),
    (SyncRequestType.AlbumsV1, "album", SyncEntityType.AlbumV1),
    (SyncRequestType.AlbumToAssetsV1, "album_asset", SyncEntityType.AlbumToAssetV1),
    (SyncRequestType.AssetExifsV1, "exif", SyncEntityType.AssetExifV1),
    (SyncRequestType.PeopleV1, "person", SyncEntityType.PersonV1),
    (SyncRequestType.AssetFacesV1, "face", SyncEntityType.AssetFaceV1),
]

_EntityType: TypeAlias = (
    AssetResponse
    | AlbumResponse
    | AlbumAssetResponse
    | PersonResponse
    | FaceResponse
    | ExifResponse
)


def _to_ack_string(
    entity_type: SyncEntityType,
    cursor: str,
) -> str:
    """
    Convert entity type and cursor to ack string.

    Ack format for immich-adapter: "SyncEntityType|cursor|"

    Args:
        entity_type: The sync entity type
        cursor: The opaque v2 events cursor

    Returns:
        Formatted ack string
    """
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
        cursor: The opaque v2 events cursor for checkpointing

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
    event: EventV2Data,
) -> tuple[str, SyncEntityType] | None:
    """
    Convert a v2 delete event to an Immich delete sync event JSON line.

    Validates that event.entity_type matches expectations for the delete
    event_type. Logs a warning and skips if there's a mismatch.

    Args:
        event: The v2 event with a delete event_type

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

    logger.warning(
        "Unhandled delete event type in _make_delete_sync_event",
        extra={
            "event_type": event.event_type,
            "entity_id": event.entity_id,
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
        cursor: The v2 event cursor for the ack string
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
) -> dict[str, _EntityType]:
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
        Dict mapping entity_id -> entity object
    """
    if not entity_ids:
        return {}

    unique_ids = list(dict.fromkeys(entity_ids))  # Deduplicate, preserve order
    result: dict[str, _EntityType] = {}

    for chunk in _batched(unique_ids, FETCH_BATCH_SIZE):
        if gumnut_entity_type == "asset":
            page = gumnut_client.assets.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page})

        elif gumnut_entity_type == "album":
            page = gumnut_client.albums.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page})

        elif gumnut_entity_type == "person":
            page = gumnut_client.people.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page})

        elif gumnut_entity_type == "face":
            page = gumnut_client.faces.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page})

        elif gumnut_entity_type == "album_asset":
            page = gumnut_client.album_assets.list(ids=chunk, limit=len(chunk))
            result.update({entity.id: entity for entity in page})

        elif gumnut_entity_type == "exif":
            # Exif is 1:1 with asset; v2 exif events use entity_id = asset_id
            page = gumnut_client.assets.list(ids=chunk, limit=len(chunk))
            for asset in page:
                if asset.exif:
                    result[asset.exif.asset_id] = asset.exif

        else:
            logger.warning(
                "Unknown entity type in _fetch_entities_map",
                extra={"gumnut_entity_type": gumnut_entity_type},
            )
            return {}

    return result


async def _stream_entity_type(
    gumnut_client: Gumnut,
    gumnut_entity_type: str,
    sync_entity_type: SyncEntityType,
    owner_id: str,
    checkpoint: Checkpoint | None,
    sync_started_at: datetime,
) -> AsyncGenerator[tuple[str, int], None]:
    """
    Stream events for a single entity type using the v2 events API.

    Fetches lightweight v2 events, then batch-fetches full entities for
    upsert events. Delete events are converted directly to Immich delete
    sync events.

    Args:
        gumnut_client: The Gumnut API client
        gumnut_entity_type: The entity type string for the Gumnut API (e.g., "asset")
        sync_entity_type: The Immich sync entity type (e.g., SyncEntityType.AssetV1)
        owner_id: The owner UUID string
        checkpoint: The checkpoint with cursor (None for full sync)
        sync_started_at: Upper bound for the query window

    Yields:
        Tuples of (json_line, count) for each event
    """
    last_cursor = checkpoint.cursor if checkpoint else None
    count = 0

    while True:
        # Build params for v2 events API
        params: dict[str, Any] = {
            "created_at_lt": sync_started_at,
            "entity_types": gumnut_entity_type,
            "limit": EVENTS_PAGE_SIZE,
        }
        if last_cursor is not None:
            params["after_cursor"] = last_cursor

        events_response = gumnut_client.events_v2.get(**params)

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
        entities_map = _fetch_entities_map(
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
                # Delete event — convert directly
                result = _make_delete_sync_event(event)
                if result:
                    json_line, _ = result
                    yield json_line, 1
                    count += 1
            else:
                # Upsert event — look up fetched entity
                entity = entities_map.get(event.entity_id)
                if entity is None:
                    # Entity was deleted between event and fetch — skip
                    continue
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


async def generate_sync_stream(
    gumnut_client: Gumnut,
    request: SyncStreamDto,
    checkpoint_map: dict[SyncEntityType, Checkpoint],
) -> AsyncGenerator[str, None]:
    """
    Generate sync stream as JSON Lines (newline-delimited JSON).

    Uses the photos-api v2 events endpoint to fetch lightweight event records
    in priority order, then batch-fetches full entities for upsert events.

    Each entity type uses its own checkpoint with an opaque cursor for
    cursor-based pagination.

    Each line is a JSON object with: type, data, and ack (checkpoint cursor).
    """
    try:
        # Get current user for owner_id
        current_user = gumnut_client.users.me()
        owner_id = str(safe_uuid_from_user_id(current_user.id))

        requested_types = set(request.types)

        logger.info(
            f"Starting sync stream with {len(requested_types)} entity types",
            extra={
                "user_id": owner_id,
                "types": [t.value for t in requested_types],
                "reset": request.reset,
                "checkpoints": len(checkpoint_map),
            },
        )

        # User/auth-user entities don't go through v2 events.
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

        # Process each entity type in order, using its own checkpoint
        for request_type, gumnut_entity_type, sync_entity_type in _SYNC_TYPE_ORDER:
            if request_type not in requested_types:
                continue

            # Get checkpoint for this entity type
            checkpoint = checkpoint_map.get(sync_entity_type)

            # Stream events for this entity type
            async for event_line, count in _stream_entity_type(
                gumnut_client,
                gumnut_entity_type,
                sync_entity_type,
                owner_id,
                checkpoint,
                sync_started_at,
            ):
                yield event_line
                event_counts[sync_entity_type.value] = (
                    event_counts.get(sync_entity_type.value, 0) + count
                )
                total_events += count

        # Log summary
        if total_events > 0:
            logger.info(
                "Streamed events from photos-api",
                extra={
                    "user_id": owner_id,
                    "total_events": total_events,
                    "event_counts": event_counts,
                },
            )

        # Stream completion event
        yield _make_sync_event(SyncEntityType.SyncCompleteV1, {}, "")
        logger.info("Sync stream completed", extra={"user_id": owner_id})

    except Exception as e:
        logger.error(f"Error generating sync stream: {str(e)}", exc_info=True)
        error_event = {
            "type": "Error",
            "data": {"message": "Internal sync error occurred"},
            "ack": str(uuid.uuid4()),
        }
        yield json.dumps(error_event) + "\n"


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
