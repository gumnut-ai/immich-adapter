"""Sync stream generation — two-phase event streaming with FK integrity.

Orchestrates event fetching, entity hydration, and stream generation.
Delegates to submodules for event construction (events), entity fetching
(entity_fetch), and FK validation (fk_integrity).
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from collections.abc import Iterator
from typing import Any, AsyncGenerator

from gumnut import AsyncGumnut
from gumnut.types.album_response import AlbumResponse
from gumnut.types.face_response import FaceResponse

from routers.immich_models import (
    SyncEntityType,
    SyncRequestType,
    SyncStreamDto,
)
from routers.utils.gumnut_id_conversion import safe_uuid_from_user_id
from services.checkpoint_store import Checkpoint

from routers.api.sync.converters import (
    gumnut_user_to_sync_auth_user_v1,
    gumnut_user_to_sync_user_v1,
)
from routers.api.sync.entity_fetch import fetch_entities_map
from routers.api.sync.events import (
    convert_entity_to_sync_event,
    make_delete_sync_event,
    make_sync_event,
    to_ack_string,
)
from routers.api.sync.fk_integrity import (
    SyncStreamStats,
    check_fk_references,
    null_deleted_fk_references,
    payload_override,
)

logger = logging.getLogger(__name__)

# Page size for events API pagination
EVENTS_PAGE_SIZE = 500

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

# Supported SyncRequestTypes (used to detect unsupported types requested by client)
_SUPPORTED_REQUEST_TYPES: frozenset[SyncRequestType] = frozenset(
    {request_type for request_type, _, _ in _SYNC_TYPE_ORDER}
    | {SyncRequestType.AuthUsersV1, SyncRequestType.UsersV1}
)


async def _stream_entity_type(
    gumnut_client: AsyncGumnut,
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

        events_response = await gumnut_client.events.get(**params)

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
        entities_map, missing_ids = await fetch_entities_map(
            gumnut_client, gumnut_entity_type, upsert_ids
        )

        # Track entity IDs that were requested but not returned (deleted/404)
        not_returned = set(upsert_ids) - entities_map.keys()
        if not_returned:
            stats.not_found_ids[gumnut_entity_type].update(not_returned)

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
                result = make_delete_sync_event(event)
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
                    # fetch_entities_map when an asset lacks exif data.
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
                ):
                    should_apply, person_id = payload_override(
                        event.payload, "person_id"
                    )
                    if should_apply and entity.person_id != person_id:
                        entity = entity.model_copy(update={"person_id": person_id})

                # album_updated events carry the causally-consistent
                # album_cover_asset_id in the event payload. Use it
                # instead of the entity's current state, which is
                # computed at fetch time (oldest asset in album) and may
                # reference an asset added after the event was recorded
                # — potentially outside the client's sync window.
                if (
                    sync_entity_type == SyncEntityType.AlbumV1
                    and event.event_type == "album_updated"
                    and isinstance(entity, AlbumResponse)
                    and isinstance(event.payload, dict)
                ):
                    should_apply, cover_id = payload_override(
                        event.payload, "album_cover_asset_id"
                    )
                    if should_apply and entity.album_cover_asset_id != cover_id:
                        entity = entity.model_copy(
                            update={"album_cover_asset_id": cover_id}
                        )

                # Null FK fields that reference entities confirmed deleted
                # (returned 404 during fetch). Uses FK_REFERENCES to
                # discover which fields to check. Skipped when the
                # referenced entity type has a checkpoint.
                entity = null_deleted_fk_references(
                    gumnut_entity_type,
                    entity,
                    stats,
                    checkpoint_map,
                    event.event_type,
                    event.cursor,
                )

                # Track streamed entity ID before FK check so the current
                # entity is visible to its own reference validation
                entity_id = getattr(entity, "id", None)
                if entity_id is not None:
                    stats.streamed_ids[gumnut_entity_type].add(entity_id)

                # Check FK references before yielding
                check_fk_references(
                    gumnut_entity_type,
                    entity,
                    stats,
                    checkpoint_map,
                    event.cursor,
                )

                json_line = convert_entity_to_sync_event(
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
) -> Iterator[tuple[str, SyncEntityType]]:
    """
    Yield buffered delete events in reverse FK dependency order.

    Groups deletes by SyncEntityType and yields them in ``_DELETE_TYPE_ORDER``
    (children before parents), preserving chronological order within each type.
    Any delete types not in ``_DELETE_TYPE_ORDER`` are yielded at the end.

    Args:
        delete_buffer: List of (json_line, SyncEntityType) tuples

    Yields:
        (json_line, SyncEntityType) tuples in reverse FK dependency order
    """
    if not delete_buffer:
        return

    # Group by SyncEntityType, preserving insertion (chronological) order
    groups: dict[SyncEntityType, list[str]] = defaultdict(list)
    for json_line, delete_type in delete_buffer:
        groups[delete_type].append(json_line)

    # Yield in _DELETE_TYPE_ORDER (reverse FK dependency)
    for delete_type in _DELETE_TYPE_ORDER:
        for json_line in groups.pop(delete_type, []):
            yield json_line, delete_type

    # Defensive: yield any remaining types not in _DELETE_TYPE_ORDER
    for delete_type, lines in groups.items():
        logger.warning(
            "Delete type not in _DELETE_TYPE_ORDER, emitting in arbitrary order",
            extra={"delete_type": delete_type.value, "count": len(lines)},
        )
        for json_line in lines:
            yield json_line, delete_type


async def generate_sync_stream(
    gumnut_client: AsyncGumnut,
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
        current_user = await gumnut_client.users.me()
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
                yield make_sync_event(
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
                yield make_sync_event(
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
        if delete_buffer:
            logger.info(
                "Emitting buffered deletes — cursors may be lower than "
                "last upsert cursor; deletes could be lost if client resumes from "
                "the upsert checkpoint",
                extra={
                    "user_id": owner_id,
                    "buffered_deletes": len(delete_buffer),
                },
            )
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
        yield make_sync_event(SyncEntityType.SyncCompleteV1, {}, "complete")
        logger.info("Sync stream completed", extra={"user_id": owner_id})

    except Exception:
        logger.error("Error generating sync stream", exc_info=True)


async def generate_reset_stream() -> AsyncGenerator[str, None]:
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
                "ack": to_ack_string(SyncEntityType.SyncResetV1, "reset"),
            }
        )
        + "\n"
    )
