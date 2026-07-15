import logging
from typing import Any, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse

from gumnut import AsyncGumnut
from gumnut.types import PersonResponse

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.utils.bulk import classify_bulk_item_call
from routers.utils.cdn_client import stream_from_cdn
from routers.utils.concurrency import gather_with_concurrency
from routers.utils.error_mapping import log_upstream_response
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.immich_models import (
    AssetFaceUpdateDto,
    AssetFaceUpdateItem,
    BulkIdResponseDto,
    BulkIdsDto,
    BulkIdErrorReason,
    MergePersonDto,
    PeopleResponseDto,
    PeopleUpdateDto,
    PeopleUpdateItem,
    PersonCreateDto,
    PersonResponseDto,
    PersonStatisticsResponseDto,
    PersonUpdateDto,
)
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_person_id,
)
from routers.utils.person_conversion import convert_gumnut_person_to_immich

router = APIRouter(
    prefix="/api/people",
    tags=["people"],
    responses={404: {"description": "Not found"}},
)

logger = logging.getLogger(__name__)


async def _resolve_thumbnail_face_id(
    client: AsyncGumnut,
    gumnut_person_id: str,
    feature_face_asset_id: UUID,
) -> str:
    """Resolve an Immich featureFaceAssetId to a Gumnut thumbnail_face_id.

    Immich identifies feature faces by asset ID, while Gumnut uses face IDs.
    This finds the face belonging to the given person on the given asset.
    """
    gumnut_asset_id = uuid_to_gumnut_asset_id(feature_face_asset_id)
    faces_page = await client.faces.list(
        person_id=gumnut_person_id,
        asset_id=gumnut_asset_id,
        limit=1,
    )
    if not faces_page.data:
        raise HTTPException(
            status_code=400,
            detail=f"No face found for this person on asset {feature_face_asset_id}",
        )
    return faces_page.data[0].id


def _immich_people_sort_key(person: PersonResponse) -> tuple:
    """Sort key matching Immich's default people ordering.

    Immich orders people by:
    1. Hidden status (visible first)
    2. Favorite status (favorites first)
    3. Named people first (non-empty name before empty/null)
    4. Asset count descending (most photos first)
    5. Name alphabetically (nulls last)
    6. Creation date ascending (oldest first, as tiebreaker)
    """
    normalized_name = (person.name or "").strip()
    has_no_name = normalized_name == ""
    asset_count = person.asset_count or 0
    return (
        person.is_hidden,  # False < True → visible first
        not person.is_favorite,  # True first → negate so favorites sort first
        has_no_name,  # False < True → named people first
        -asset_count,  # Negate for descending order
        normalized_name.casefold(),  # Alphabetical (unnamed all sort as "")
        person.created_at,  # Ascending (oldest first)
        person.id,  # Deterministic tiebreaker for stable pagination
    )


@router.post("", status_code=201)
async def create_person(
    person_data: PersonCreateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> PersonResponseDto:
    """
    Create a new person.
    """
    gumnut_person = await client.people.create(
        name=person_data.name,
        birth_date=person_data.birthDate,
        is_favorite=person_data.isFavorite,
        is_hidden=person_data.isHidden,
    )
    return convert_gumnut_person_to_immich(gumnut_person)


@router.put("")
async def update_people(
    people_data: PeopleUpdateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[BulkIdResponseDto]:
    """
    Update multiple people by their ids.
    """
    return await gather_with_concurrency(
        [_update_one_person(client, person_item) for person_item in people_data.people]
    )


async def _update_one_person(
    client: AsyncGumnut,
    person_item: PeopleUpdateItem,
) -> BulkIdResponseDto:
    """Update a single person; map any error to a per-item BulkIdResponseDto.

    Errors are caught here (not surfaced via the gather helper) so a single
    bad item can't abort the rest of the batch — Immich clients expect a
    complete results list. The SDK-error tail (`APIStatusError` /
    `GumnutError`, raised from either `_resolve_thumbnail_face_id`'s inner
    `client.faces.list` or the final `client.people.update`) is delegated to
    `classify_bulk_item_call`, mirroring the per-chunk policy used by
    `chunked_per_item_bulk` for chunked bulk endpoints. The exception
    specific to this endpoint (`HTTPException` from
    `_resolve_thumbnail_face_id`'s "missing face" branch) stays here — it
    is not an SDK error, so `classify_bulk_item_call` lets it propagate.
    """
    log_extra = {"person_id": person_item.id}

    # v3 types `PeopleUpdateItem.id` as UUID, so malformed ids are rejected
    # with a 422 at the request boundary before reaching this handler.
    gumnut_person_id = uuid_to_gumnut_person_id(person_item.id)

    try:
        sdk_error = await classify_bulk_item_call(
            _do_person_update(client, gumnut_person_id, person_item),
            error_enum=BulkIdErrorReason,
            log_context="update_people",
            log_extra=log_extra,
        )
    except HTTPException as he:
        # Single source: `_resolve_thumbnail_face_id` raises HTTPException(400)
        # for "no face found" — maps to `unknown` (no enum bucket fits a
        # missing-face signal). SDK errors from `client.faces.list` /
        # `client.people.update` are caught above by `classify_bulk_item_call`.
        log_upstream_response(
            logger,
            context="update_people",
            status_code=he.status_code,
            message=(
                f"HTTPException in bulk person update for {person_item.id}: "
                f"{he.status_code} {he.detail}"
            ),
            extra=log_extra,
        )
        return BulkIdResponseDto(
            id=person_item.id, success=False, error=BulkIdErrorReason.unknown
        )

    if sdk_error is not None:
        return BulkIdResponseDto(id=person_item.id, success=False, error=sdk_error)
    return BulkIdResponseDto(id=person_item.id, success=True, error=None)


async def _do_person_update(
    client: AsyncGumnut,
    gumnut_person_id: str,
    person_item: PeopleUpdateItem,
) -> None:
    """Build update kwargs from the Immich patch and apply via the SDK.

    Wrapped by `classify_bulk_item_call` so SDK errors from either
    `_resolve_thumbnail_face_id` (which calls `client.faces.list`) or the
    final `client.people.update` are caught and classified uniformly.
    `HTTPException` from the missing-face branch propagates to the caller
    for endpoint-specific mapping.
    """
    update_kwargs: dict[str, Any] = {}
    if person_item.name is not None:
        update_kwargs["name"] = person_item.name
    if person_item.birthDate is not None:
        update_kwargs["birth_date"] = person_item.birthDate
    if person_item.isFavorite is not None:
        update_kwargs["is_favorite"] = person_item.isFavorite
    if person_item.isHidden is not None:
        update_kwargs["is_hidden"] = person_item.isHidden
    if person_item.featureFaceAssetId is not None:
        update_kwargs["thumbnail_face_id"] = await _resolve_thumbnail_face_id(
            client, gumnut_person_id, person_item.featureFaceAssetId
        )
    await client.people.update(person_id=gumnut_person_id, **update_kwargs)


@router.put("/{id}")
async def update_person(
    id: UUID,
    person_data: PersonUpdateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> PersonResponseDto:
    """
    Update a person by their id.
    """
    update_kwargs = {}
    gumnut_person_id = uuid_to_gumnut_person_id(id)
    if person_data.name is not None:
        update_kwargs["name"] = person_data.name
    if person_data.birthDate is not None:
        update_kwargs["birth_date"] = person_data.birthDate
    if person_data.isFavorite is not None:
        update_kwargs["is_favorite"] = person_data.isFavorite
    if person_data.isHidden is not None:
        update_kwargs["is_hidden"] = person_data.isHidden
    if person_data.featureFaceAssetId is not None:
        update_kwargs["thumbnail_face_id"] = await _resolve_thumbnail_face_id(
            client, gumnut_person_id, person_data.featureFaceAssetId
        )

    gumnut_person = await client.people.update(
        person_id=gumnut_person_id, **update_kwargs
    )
    return convert_gumnut_person_to_immich(gumnut_person)


@router.get("")
async def get_all_people(
    closestAssetId: UUID = Query(default=None),
    closestPersonId: UUID = Query(default=None),
    page: int = Query(default=1, ge=1, type="number"),
    size: int = Query(default=500, ge=1, le=1000, type="number"),
    withHidden: bool = Query(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> PeopleResponseDto:
    """
    Get all people with optional pagination and filtering.
    """
    gumnut_people = client.people.list(
        name_filter="all", limit=GUMNUT_API_MAX_PAGE_SIZE
    )
    all_people = [p async for p in gumnut_people]

    # Count hidden before filtering so the response includes the total
    hidden_count = sum(1 for p in all_people if p.is_hidden)

    if withHidden is False:
        all_people = [p for p in all_people if not p.is_hidden]

    all_people.sort(key=_immich_people_sort_key)

    total_count = len(all_people)

    start_index = (page - 1) * size
    end_index = start_index + size
    page_people = all_people[start_index:end_index]
    has_next_page = end_index < total_count

    return PeopleResponseDto(
        people=[convert_gumnut_person_to_immich(p) for p in page_people],
        hasNextPage=has_next_page,
        total=total_count,
        hidden=hidden_count,
    )


@router.delete("", status_code=204)
async def delete_people(
    request: BulkIdsDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Delete multiple people by their ids.

    Per-item errors propagate to the global ``GumnutError`` handler — the
    endpoint contract is 204-on-all-success, with the first failure aborting
    the batch (gather cancels pending siblings).
    """
    await gather_with_concurrency(
        [
            client.people.delete(uuid_to_gumnut_person_id(person_id))
            for person_id in request.ids
        ]
    )
    return Response(status_code=204)


@router.get(
    "/{id}/thumbnail",
    responses={
        200: {
            "description": "Any binary media",
            "content": {
                "image/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
                "video/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
                "*/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
            },
        }
    },
)
async def get_thumbnail(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> StreamingResponse:
    """
    Get a thumbnail for a person.
    Retrieves person metadata and streams the thumbnail from CDN.
    """
    gumnut_person = await client.people.retrieve(uuid_to_gumnut_person_id(id))

    if not gumnut_person.asset_urls or "thumbnail" not in gumnut_person.asset_urls:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Person thumbnail not available",
        )

    variant_info = gumnut_person.asset_urls["thumbnail"]
    return await stream_from_cdn(variant_info.url, variant_info.mimetype)


@router.get("/{id}")
async def get_person(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> PersonResponseDto:
    """
    Get details for a specific person.
    """
    gumnut_person = await client.people.retrieve(uuid_to_gumnut_person_id(id))
    return convert_gumnut_person_to_immich(gumnut_person)


@router.get("/{id}/statistics")
async def get_person_statistics(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> PersonStatisticsResponseDto:
    """
    Get asset statistics for a specific person.
    """
    gumnut_person = await client.people.retrieve(uuid_to_gumnut_person_id(id))
    return PersonStatisticsResponseDto(assets=gumnut_person.asset_count or 0)


@router.delete("/{id}", status_code=204)
async def delete_person(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Delete a person by their id.
    """
    await client.people.delete(uuid_to_gumnut_person_id(id))
    return Response(status_code=204)


@router.post("/{id}/merge")
async def merge_person(
    id: UUID,
    request: MergePersonDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[BulkIdResponseDto]:
    """Merge one or more source people into the target person at ``{id}``."""
    if not request.ids:
        return []

    if id in request.ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot merge a person into themselves",
        )

    await client.people.merge(
        uuid_to_gumnut_person_id(id),
        source_person_ids=[
            uuid_to_gumnut_person_id(source_uuid) for source_uuid in request.ids
        ],
    )

    return [
        BulkIdResponseDto(id=source_uuid, success=True, error=None)
        for source_uuid in request.ids
    ]


@router.put("/{id}/reassign")
async def reassign_faces(
    id: UUID,
    request: AssetFaceUpdateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[PersonResponseDto]:
    """Reassign faces to a person.

    The URL {id} is the target person (who to reassign faces TO).
    For each item in the request, finds the face belonging to the source person
    (body personId) on the given asset and reassigns it to the target person
    (URL {id}). Returns the target person if any faces were reassigned.
    """
    if not request.data:
        return []

    gumnut_target_person_id = uuid_to_gumnut_person_id(id)

    # Validate and cache the target person before modifying any faces
    gumnut_person = await client.people.retrieve(gumnut_target_person_id)
    target_person = convert_gumnut_person_to_immich(gumnut_person)

    reassign_results = await gather_with_concurrency(
        [
            _reassign_one_pair(client, item, gumnut_target_person_id)
            for item in request.data
        ]
    )

    return [target_person] if any(reassign_results) else []


async def _reassign_one_pair(
    client: AsyncGumnut,
    item: AssetFaceUpdateItem,
    gumnut_target_person_id: str,
) -> bool:
    """Reassign one (asset, sourcePerson) pair's face(s); return True if any moved.

    The inner per-face loop stays sequential — request.data items typically
    yield 0 or 1 face, so the outer fan-out captures the win.
    """
    gumnut_asset_id = uuid_to_gumnut_asset_id(item.assetId)
    gumnut_source_person_id = uuid_to_gumnut_person_id(item.personId)

    faces = [
        f
        async for f in client.faces.list(
            person_id=gumnut_source_person_id,
            asset_id=gumnut_asset_id,
        )
    ]
    if not faces:
        logger.warning(
            "No face found for source person on asset, skipping",
            extra={
                "source_person_id": gumnut_source_person_id,
                "target_person_id": gumnut_target_person_id,
                "asset_id": gumnut_asset_id,
            },
        )
        return False

    for face in faces:
        await client.faces.update(face.id, person_id=gumnut_target_person_id)
    return True
