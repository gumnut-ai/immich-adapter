"""FK reference validation, payload overrides, and sync stream stats."""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from routers.immich_models import SyncEntityType
from routers.api.sync.types import EntityType
from services.checkpoint_store import Checkpoint

logger = logging.getLogger(__name__)

# FK references: gumnut_entity_type -> [(attribute_name, referenced_gumnut_entity_type)]
FK_REFERENCES: dict[str, list[tuple[str, str]]] = {
    "face": [("person_id", "person"), ("asset_id", "asset")],
    "album_asset": [("album_id", "album"), ("asset_id", "asset")],
    "album": [("album_cover_asset_id", "asset")],
}

# Map gumnut entity type -> SyncEntityType(s) for FK checkpoint lookups.
# Derived from the canonical type order in stream.py; duplicated here to
# avoid a circular import (fk_integrity is imported by stream).
# Entity types with multiple versions (e.g., face V1/V2) list all variants
# so checkpoint lookups match regardless of which version the client synced.
_GUMNUT_TYPE_TO_SYNC_TYPES: dict[str, list[SyncEntityType]] = {
    "asset": [SyncEntityType.AssetV1],
    "album": [SyncEntityType.AlbumV1],
    "album_asset": [SyncEntityType.AlbumToAssetV1],
    "exif": [SyncEntityType.AssetExifV1],
    "person": [SyncEntityType.PersonV1],
    "face": [SyncEntityType.AssetFaceV1, SyncEntityType.AssetFaceV2],
}


def payload_override(payload: dict[str, Any], key: str) -> tuple[bool, str | None]:
    """Check an event payload for a causally-consistent FK override.

    Returns (should_apply, value) where should_apply is True if the payload
    contains a valid override value (None or non-empty string) for the given key.
    Logs a warning for unexpected types so bad payloads are visible, not silently
    ignored.
    """
    if key not in payload:
        return False, None
    value = payload[key]
    if value is None:
        return True, None
    if isinstance(value, str):
        value = value.strip()
        return (True, value) if value else (False, None)
    logger.warning(
        "Unexpected payload type for %s: %r (type=%s), skipping override",
        key,
        value,
        type(value).__name__,
    )
    return False, None


@dataclass
class SyncStreamStats:
    """Tracks streamed entity IDs and skip counts during sync stream generation."""

    streamed_ids: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    not_found_ids: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    entity_not_found_skips: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    delete_event_skips: int = 0
    buffered_deletes: int = 0
    fk_warnings: int = 0


def check_fk_references(
    gumnut_entity_type: str,
    entity: EntityType,
    stats: SyncStreamStats,
    checkpoint_map: dict[SyncEntityType, Checkpoint],
    cursor: str,
) -> None:
    """Warn if entity references IDs not seen in this sync for fully-synced entity types.

    Only warns when the referenced entity type has no checkpoint (i.e., it was
    fully synced in this cycle), since a prior checkpoint means the referenced
    entity may have been synced in an earlier cycle.
    """
    refs = FK_REFERENCES.get(gumnut_entity_type)
    if not refs:
        return

    for attr_name, ref_type in refs:
        ref_id = getattr(entity, attr_name, None)
        if ref_id is None:
            continue

        # If the referenced entity type has a checkpoint, skip the check —
        # the referenced entity may have been synced in a prior cycle
        ref_sync_types = _GUMNUT_TYPE_TO_SYNC_TYPES.get(ref_type)
        if ref_sync_types and any(t in checkpoint_map for t in ref_sync_types):
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


def null_deleted_fk_references(
    gumnut_entity_type: str,
    entity: EntityType,
    stats: SyncStreamStats,
    checkpoint_map: dict[SyncEntityType, Checkpoint],
    event_type: str,
    cursor: str,
) -> EntityType:
    """Null FK fields that reference entities confirmed deleted (404 during fetch).

    Uses FK_REFERENCES to discover which fields to check. Skips fields whose
    referenced entity type has a checkpoint — the entity may exist on the
    client from a prior sync cycle.

    Returns the (possibly updated) entity.
    """
    refs = FK_REFERENCES.get(gumnut_entity_type)
    if not refs:
        return entity

    updates: dict[str, None] = {}
    for attr_name, ref_type in refs:
        ref_id = getattr(entity, attr_name, None)
        if ref_id is None:
            continue

        ref_sync_types = _GUMNUT_TYPE_TO_SYNC_TYPES.get(ref_type)
        if ref_sync_types and any(t in checkpoint_map for t in ref_sync_types):
            continue

        if ref_id in stats.not_found_ids.get(ref_type, set()):
            logger.info(
                "Nulling FK reference to deleted entity",
                extra={
                    "entity_type": gumnut_entity_type,
                    "entity_id": getattr(entity, "id", None),
                    "reference_field": attr_name,
                    "referenced_type": ref_type,
                    "deleted_ref_id": ref_id,
                    "event_type": event_type,
                    "cursor": cursor,
                },
            )
            updates[attr_name] = None

    if updates:
        entity = entity.model_copy(update=updates)

    return entity
