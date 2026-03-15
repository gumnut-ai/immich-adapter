"""Sync event construction — converting entities and events to JSON lines."""

import json
import logging
from typing import Any, cast

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
)
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_album_id,
    safe_uuid_from_asset_id,
    safe_uuid_from_face_id,
    safe_uuid_from_person_id,
)

from routers.api.sync.converters import (
    gumnut_album_asset_to_sync_album_to_asset_v1,
    gumnut_album_to_sync_album_v1,
    gumnut_asset_to_sync_asset_v1,
    gumnut_exif_to_sync_exif_v1,
    gumnut_face_to_sync_face_v1,
    gumnut_person_to_sync_person_v1,
)
from routers.api.sync.types import EntityType

logger = logging.getLogger(__name__)

# Expected entity_type for each delete event_type
_DELETE_EVENT_ENTITY_TYPES: dict[str, str] = {
    "asset_deleted": "asset",
    "album_deleted": "album",
    "person_deleted": "person",
    "face_deleted": "face",
    "album_asset_removed": "album_asset",
}


def to_ack_string(
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


def make_sync_event(
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
    ack = to_ack_string(entity_type, cursor)

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


def make_delete_sync_event(
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
            make_sync_event(
                SyncEntityType.AssetDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.AssetDeleteV1,
        )

    elif event.event_type == "album_deleted":
        data = SyncAlbumDeleteV1(albumId=str(safe_uuid_from_album_id(event.entity_id)))
        return (
            make_sync_event(
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
            make_sync_event(
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
            make_sync_event(
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
            make_sync_event(
                SyncEntityType.AlbumToAssetDeleteV1,
                data.model_dump(mode="json"),
                event.cursor,
            ),
            SyncEntityType.AlbumToAssetDeleteV1,
        )

    logger.warning(
        "Unhandled delete event type in make_delete_sync_event",
        extra={
            "event_type": event.event_type,
            "entity_id": event.entity_id,
            "cursor": event.cursor,
        },
    )
    return None


def convert_entity_to_sync_event(
    gumnut_entity_type: str,
    entity: EntityType,
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
    sync_model: Any
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

    return make_sync_event(
        sync_entity_type,
        sync_model.model_dump(mode="json"),
        cursor,
    )
