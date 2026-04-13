from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from uuid import UUID
import logging

from gumnut import AsyncGumnut
from gumnut.types import PersonResponse

from routers.utils.cdn_client import stream_from_cdn
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.error_mapping import map_gumnut_error, check_for_error_by_code
from routers.immich_models import (
    AssetFaceUpdateDto,
    BulkIdResponseDto,
    BulkIdsDto,
    Error1,
    MergePersonDto,
    PeopleResponseDto,
    PeopleUpdateDto,
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
    try:
        gumnut_person = await client.people.create(
            name=person_data.name,
            birth_date=person_data.birthDate,
            is_favorite=person_data.isFavorite,
            is_hidden=person_data.isHidden,
        )

        return convert_gumnut_person_to_immich(gumnut_person)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to create person") from e


@router.put("")
async def update_people(
    people_data: PeopleUpdateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[BulkIdResponseDto]:
    """
    Update multiple people by their ids.
    """
    results = []

    for person_item in people_data.people:
        try:
            # Update the person using Gumnut SDK - only pass parameters that are not None
            update_kwargs = {}
            gumnut_person_id = uuid_to_gumnut_person_id(
                UUID(person_item.id)
            )  # immich openapi specs switch between str and UUID for people id
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

            await client.people.update(
                person_id=gumnut_person_id,
                **update_kwargs,
            )

            results.append(
                BulkIdResponseDto(id=person_item.id, success=True, error=None)
            )

        except HTTPException as he:
            # Map adapter-raised HTTPExceptions to per-item failures so the
            # bulk endpoint never aborts mid-batch (Immich clients expect a
            # complete results list).
            if he.status_code == 404:
                error = Error1.not_found
            elif he.status_code in (401, 403):
                error = Error1.no_permission
            else:
                error = Error1.unknown
            results.append(
                BulkIdResponseDto(id=person_item.id, success=False, error=error)
            )
            logger.warning(
                "HTTPException in bulk person update for %s: %s %s",
                person_item.id,
                he.status_code,
                he.detail,
            )
        except Exception as e:
            error_msg = str(e).lower()
            if check_for_error_by_code(e, 404) or "not found" in error_msg:
                results.append(
                    BulkIdResponseDto(
                        id=person_item.id, success=False, error=Error1.not_found
                    )
                )
            elif check_for_error_by_code(e, 401) or "invalid api key" in error_msg:
                results.append(
                    BulkIdResponseDto(
                        id=person_item.id, success=False, error=Error1.no_permission
                    )
                )
            else:
                results.append(
                    BulkIdResponseDto(
                        id=person_item.id, success=False, error=Error1.unknown
                    )
                )

    return results


@router.put("/{id}")
async def update_person(
    id: UUID,
    person_data: PersonUpdateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> PersonResponseDto:
    """
    Update a person by their id.
    """
    try:
        # Update the person using Gumnut SDK - only pass parameters that are not None
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

    except HTTPException:
        raise
    except Exception as e:
        raise map_gumnut_error(e, "Failed to update person") from e


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
    try:
        # Get all people from Gumnut
        gumnut_people = client.people.list(name_filter="all")
        all_people = [p async for p in gumnut_people]

        # Count hidden before filtering so the response includes the total
        hidden_count = sum(1 for p in all_people if p.is_hidden)

        # Filter hidden people first (before sorting and pagination)
        if withHidden is False:
            all_people = [p for p in all_people if not p.is_hidden]

        # Sort to match Immich's expected ordering
        all_people.sort(key=_immich_people_sort_key)

        total_count = len(all_people)

        # Apply pagination after filtering and sorting
        start_index = (page - 1) * size
        end_index = start_index + size
        page_people = all_people[start_index:end_index]
        has_next_page = end_index < total_count

        converted_people = [convert_gumnut_person_to_immich(p) for p in page_people]

        return PeopleResponseDto(
            people=converted_people,
            hasNextPage=has_next_page,
            total=total_count,
            hidden=hidden_count,
        )

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch people") from e


@router.delete("", status_code=204)
async def delete_people(
    request: BulkIdsDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Delete multiple people by their ids.
    """
    try:
        for person_id in request.ids:
            await client.people.delete(uuid_to_gumnut_person_id(person_id))

        return Response(status_code=204)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to delete people") from e


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
    try:
        gumnut_person = await client.people.retrieve(uuid_to_gumnut_person_id(id))

        if (
            not gumnut_person
            or not gumnut_person.asset_urls
            or "thumbnail" not in gumnut_person.asset_urls
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Person or thumbnail not found",
            )

        variant_info = gumnut_person.asset_urls["thumbnail"]
        cdn_url = variant_info.url
        mimetype = variant_info.mimetype

        return await stream_from_cdn(cdn_url, mimetype)

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error fetching thumbnail for person {id}: {e}")
        raise map_gumnut_error(e, "Failed to fetch person thumbnail") from e


@router.get("/{id}")
async def get_person(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> PersonResponseDto:
    """
    Get details for a specific person.
    """
    try:
        gumnut_person = await client.people.retrieve(uuid_to_gumnut_person_id(id))

        if not gumnut_person:
            raise HTTPException(status_code=404, detail="Person not found")

        return convert_gumnut_person_to_immich(gumnut_person)

    except HTTPException:
        # Re-raise HTTP exceptions (like 404 for person not found)
        raise
    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch person") from e


@router.get("/{id}/statistics")
async def get_person_statistics(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> PersonStatisticsResponseDto:
    """
    Get asset statistics for a specific person.
    """
    try:
        gumnut_assets = client.assets.list(person_id=uuid_to_gumnut_person_id(id))

        if not gumnut_assets:
            return PersonStatisticsResponseDto(assets=0)
        else:
            return PersonStatisticsResponseDto(
                assets=len([a async for a in gumnut_assets])
            )

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch person statistics") from e


@router.delete("/{id}", status_code=204)
async def delete_person(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Delete a person by their id.
    """
    try:
        await client.people.delete(uuid_to_gumnut_person_id(id))

        return Response(status_code=204)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to delete person") from e


@router.post("/{id}/merge")
async def merge_person(id: UUID, request: MergePersonDto) -> List[BulkIdResponseDto]:
    """
    Merge a person with one or more other people.
    Not supported by Gumnut, so this is a stub implementation that returns an empty list.
    """

    return []


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
    try:
        gumnut_target_person_id = uuid_to_gumnut_person_id(id)

        # Validate and cache the target person before modifying any faces
        gumnut_person = await client.people.retrieve(gumnut_target_person_id)
        target_person = convert_gumnut_person_to_immich(gumnut_person)

        any_reassigned = False
        for item in request.data:
            gumnut_asset_id = uuid_to_gumnut_asset_id(item.assetId)
            gumnut_source_person_id = uuid_to_gumnut_person_id(item.personId)

            # Find all faces belonging to the source person on this asset
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
                continue

            for face in faces:
                await client.faces.update(face.id, person_id=gumnut_target_person_id)
            any_reassigned = True

        return [target_person] if any_reassigned else []

    except HTTPException:
        raise
    except Exception as e:
        raise map_gumnut_error(e, "Failed to reassign faces") from e
