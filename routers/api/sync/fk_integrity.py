"""FK reference validation, payload overrides, and sync stream stats."""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from gumnut.types.events_response import Data as EventData
from pydantic import BaseModel

from routers.api.sync.type_order import SYNC_TYPE_ORDER
from routers.api.sync.types import EntityType
from routers.immich_models import SyncEntityType
from services.checkpoint_store import Checkpoint

logger = logging.getLogger(__name__)


class FKReference(NamedTuple):
    """Declares an entity FK and its optional event-payload override."""

    field_name: str
    referenced_type: str
    payload_override_event: str | None = None


# Each FK is declared once, including the event that can supply its
# causally-consistent value. The payload event is used to verify those IDs
# before streaming, even when the referenced entity type is not requested.
FK_REFERENCES: dict[str, list[FKReference]] = {
    "face": [
        FKReference("person_id", "person", payload_override_event="face_updated"),
        FKReference("asset_id", "asset"),
    ],
    "album_asset": [
        FKReference("album_id", "album"),
        FKReference("asset_id", "asset"),
    ],
    "album": [
        FKReference(
            "album_cover_asset_id", "asset", payload_override_event="album_updated"
        ),
    ],
}

# Checkpoint lookups accept every sync version registered for the referenced
# Gumnut entity, including derived rows such as AlbumUserV1.
_GUMNUT_TYPE_TO_SYNC_TYPES: dict[str, list[SyncEntityType]] = {}
for _, gumnut_type, sync_type in SYNC_TYPE_ORDER:
    _GUMNUT_TYPE_TO_SYNC_TYPES.setdefault(gumnut_type, []).append(sync_type)


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
    repeat_event_skips: int = 0
    buffered_deletes: int = 0
    fk_warnings: int = 0
    suppressed_face_geometry: int = 0
    absent_asset_deletes: int = 0


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

    for reference in refs:
        ref_id = getattr(entity, reference.field_name, None)
        if ref_id is None:
            continue

        # If the referenced entity type has a checkpoint, skip the check —
        # the referenced entity may have been synced in a prior cycle
        ref_sync_types = _GUMNUT_TYPE_TO_SYNC_TYPES.get(reference.referenced_type)
        if ref_sync_types and any(t in checkpoint_map for t in ref_sync_types):
            continue

        if ref_id not in stats.streamed_ids.get(reference.referenced_type, set()):
            logger.warning(
                "Entity references ID not seen in this sync",
                extra={
                    "entity_type": gumnut_entity_type,
                    "entity_id": getattr(entity, "id", None),
                    "reference_field": reference.field_name,
                    "referenced_type": reference.referenced_type,
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
    for reference in refs:
        ref_id = getattr(entity, reference.field_name, None)
        if ref_id is None:
            continue

        if ref_id in stats.not_found_ids.get(reference.referenced_type, set()):
            logger.info(
                "Nulling FK reference to deleted entity",
                extra={
                    "entity_type": gumnut_entity_type,
                    "entity_id": getattr(entity, "id", None),
                    "reference_field": reference.field_name,
                    "referenced_type": reference.referenced_type,
                    "deleted_ref_id": ref_id,
                    "event_type": event_type,
                    "cursor": cursor,
                },
            )
            updates[reference.field_name] = None

    if updates and isinstance(entity, BaseModel):
        # Only FK-bearing pydantic entities reach here — FetchedStack has no
        # FK_REFERENCES entry, so `refs` was empty and we returned above. The
        # isinstance narrows the EntityType union (which now includes the
        # non-pydantic FetchedStack) for the type checker.
        entity = entity.model_copy(update=updates)

    return entity


def extract_payload_fk_refs(
    gumnut_entity_type: str,
    events: list[EventData],
) -> dict[str, set[str]]:
    """Collect payload-referenced FK IDs from a batch of events.

    For each event whose FK metadata declares a payload override, extract the
    referenced ID and group by entity type. Returns ``{ref_type: {ref_ids...}}``.

    The caller uses this set to verify those IDs still exist in production
    before the face/album event is streamed to the client. Without this check
    the adapter would send references to entities deleted between sync cycles,
    causing FK violations on the mobile client.
    """
    references = FK_REFERENCES.get(gumnut_entity_type)
    if not references:
        return {}

    result: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if not isinstance(event.payload, dict):
            continue
        for reference in references:
            if event.event_type != reference.payload_override_event:
                continue
            should_apply, value = payload_override(event.payload, reference.field_name)
            if should_apply and value is not None:
                result[reference.referenced_type].add(value)

    return result
