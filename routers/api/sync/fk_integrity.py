"""FK reference validation, payload overrides, and sync stream stats."""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from gumnut.types.events_response import Data as EventData

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


class PayloadFKOverride(NamedTuple):
    """Declares a payload key the adapter applies as a causally-consistent FK override.

    event_type: the event_type on which this override is applied (e.g. "face_updated").
    payload_key: the key in event.payload carrying the referenced ID.
    referenced_type: the gumnut entity type that the ID refers to (e.g. "person").
    """

    event_type: str
    payload_key: str
    referenced_type: str


# Payload keys that the adapter applies as causally-consistent FK overrides
# (see payload_override). Used to collect referenced IDs up front so we can
# verify they still exist in production, regardless of whether the referenced
# entity type is being streamed in this cycle.
#
# Invariant: every (event_type, payload_key, referenced_type) entry here must
# have a matching (field=payload_key, referenced_type) entry in FK_REFERENCES
# for the same entity type — otherwise null_deleted_fk_references will not
# null the payload-overridden field when its referenced entity 404s.
PAYLOAD_FK_OVERRIDES: dict[str, list[PayloadFKOverride]] = {
    "face": [
        PayloadFKOverride(
            event_type="face_updated",
            payload_key="person_id",
            referenced_type="person",
        ),
    ],
    "album": [
        PayloadFKOverride(
            event_type="album_updated",
            payload_key="album_cover_asset_id",
            referenced_type="asset",
        ),
    ],
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
    event_type: str,
    cursor: str,
) -> EntityType:
    """Null FK fields that reference entities confirmed deleted (404 during fetch).

    Uses FK_REFERENCES to discover which fields to check. A 404 in
    ``stats.not_found_ids`` is authoritative for this sync cycle — the entity
    does not exist in production, so the client must not hold a reference to
    it regardless of any prior-cycle checkpoint (the client may have processed
    the delete in an earlier cycle and no longer has the entity locally).

    Callers are responsible for populating ``stats.not_found_ids`` for payload-
    referenced types that are not otherwise streamed (see
    ``extract_payload_fk_refs`` and the verification step in
    ``_stream_entity_type``).

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


def extract_payload_fk_refs(
    gumnut_entity_type: str,
    events: list[EventData],
) -> dict[str, set[str]]:
    """Collect payload-referenced FK IDs from a batch of events.

    For each event whose payload carries a causally-consistent FK override
    (see ``PAYLOAD_FK_OVERRIDES``), extract the referenced ID and group by
    referenced entity type. Returns ``{ref_type: {ref_ids...}}``.

    The caller uses this set to verify those IDs still exist in production
    before the face/album event is streamed to the client. Without this check
    the adapter would send references to entities deleted between sync cycles,
    causing FK violations on the mobile client.
    """
    overrides = PAYLOAD_FK_OVERRIDES.get(gumnut_entity_type)
    if not overrides:
        return {}

    result: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if not isinstance(event.payload, dict):
            continue
        for override in overrides:
            if event.event_type != override.event_type:
                continue
            should_apply, value = payload_override(event.payload, override.payload_key)
            if should_apply and value is not None:
                result[override.referenced_type].add(value)

    return result
