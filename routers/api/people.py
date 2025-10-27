from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from uuid import UUID
import logging

from gumnut import Gumnut

from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.error_mapping import map_gumnut_error, check_for_error_by_code
from routers.immich_models import (
    AssetFaceUpdateDto,
    BulkIdResponseDto,
    BulkIdsDto,
    Error2,
    MergePersonDto,
    PeopleResponseDto,
    PeopleUpdateDto,
    PersonCreateDto,
    PersonResponseDto,
    PersonStatisticsResponseDto,
    PersonUpdateDto,
)
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_person_id
from routers.utils.person_conversion import convert_gumnut_person_to_immich

router = APIRouter(
    prefix="/api/people",
    tags=["people"],
    responses={404: {"description": "Not found"}},
)

logger = logging.getLogger(__name__)


@router.post("", status_code=201)
async def create_person(
    person_data: PersonCreateDto,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> PersonResponseDto:
    """
    Create a new person.
    """
    try:
        gumnut_person = client.people.create(
            name=person_data.name,
            birth_date=person_data.birthDate,
            is_favorite=person_data.isFavorite,
            is_hidden=person_data.isHidden,
        )

        return convert_gumnut_person_to_immich(gumnut_person)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to create person")


@router.put("")
async def update_people(
    people_data: PeopleUpdateDto,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> List[BulkIdResponseDto]:
    """
    Update multiple people by their ids.
    """
    results = []

    for person_item in people_data.people:
        try:
            # Update the person using Gumnut SDK - only pass parameters that are not None
            update_kwargs = {}
            if person_item.name is not None:
                update_kwargs["name"] = person_item.name
            if person_item.birthDate is not None:
                update_kwargs["birth_date"] = person_item.birthDate
            if person_item.isFavorite is not None:
                update_kwargs["is_favorite"] = person_item.isFavorite
            if person_item.isHidden is not None:
                update_kwargs["is_hidden"] = person_item.isHidden

            client.people.update(
                person_id=uuid_to_gumnut_person_id(
                    UUID(person_item.id)
                ),  # immich openapi specs switch between str and UUID for people id
                **update_kwargs,
            )

            results.append(
                BulkIdResponseDto(id=person_item.id, success=True, error=None)
            )

        except Exception as e:
            error_msg = str(e).lower()
            if check_for_error_by_code(e, 404) or "not found" in error_msg:
                results.append(
                    BulkIdResponseDto(
                        id=person_item.id, success=False, error=Error2.not_found
                    )
                )
            elif check_for_error_by_code(e, 401) or "invalid api key" in error_msg:
                results.append(
                    BulkIdResponseDto(
                        id=person_item.id, success=False, error=Error2.no_permission
                    )
                )
            else:
                results.append(
                    BulkIdResponseDto(
                        id=person_item.id, success=False, error=Error2.unknown
                    )
                )

    return results


@router.put("/{id}")
async def update_person(
    id: UUID,
    person_data: PersonUpdateDto,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> PersonResponseDto:
    """
    Update a person by their id.
    """
    try:
        # Update the person using Gumnut SDK - only pass parameters that are not None
        update_kwargs = {}
        if person_data.name is not None:
            update_kwargs["name"] = person_data.name
        if person_data.birthDate is not None:
            update_kwargs["birth_date"] = person_data.birthDate
        if person_data.isFavorite is not None:
            update_kwargs["is_favorite"] = person_data.isFavorite
        if person_data.isHidden is not None:
            update_kwargs["is_hidden"] = person_data.isHidden

        gumnut_person = client.people.update(
            person_id=uuid_to_gumnut_person_id(id), **update_kwargs
        )

        return convert_gumnut_person_to_immich(gumnut_person)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to update person")


@router.get("")
async def get_all_people(
    closestAssetId: UUID = Query(default=None),
    closestPersonId: UUID = Query(default=None),
    page: int = Query(default=1, ge=1, type="number"),
    size: int = Query(default=500, ge=1, le=1000, type="number"),
    withHidden: bool = Query(default=None),
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> PeopleResponseDto:
    """
    Get all people with optional pagination and filtering.
    """
    try:
        # Get all people from Gumnut
        gumnut_people = client.people.list()

        # Convert to list if it's a paginated response
        people_list = list(gumnut_people)

        # Since Gumnut doesn't support the same filtering/pagination as Immich,
        # we'll implement basic logic here
        total_count = len(people_list)
        hidden_count = 0

        # Apply pagination if specified
        if page is not None and size is not None:
            start_index = (page - 1) * size
            end_index = start_index + size
            people_list = people_list[start_index:end_index]
            has_next_page = end_index < total_count

        if withHidden is False:
            people_list = [p for p in people_list if not p.is_hidden]
            hidden_count = total_count - len(people_list)
            total_count = len(people_list)
        converted_people = [convert_gumnut_person_to_immich(p) for p in people_list]

        return PeopleResponseDto(
            people=converted_people,
            hasNextPage=has_next_page,
            total=total_count,
            hidden=hidden_count,
        )

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch people")


@router.delete("", status_code=204)
async def delete_people(
    request: BulkIdsDto,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Delete multiple people by their ids.
    """
    try:
        for person_id in request.ids:
            client.people.delete(uuid_to_gumnut_person_id(person_id))

        return Response(status_code=204)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to delete people")


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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Get a thumbnail for a person.
    Uses the shared download logic with size defaulting to thumbnail if not specified.
    """
    try:
        gumnut_person = client.people.retrieve(uuid_to_gumnut_person_id(id))

        if not gumnut_person or not gumnut_person.thumbnail_face_id:
            raise HTTPException(status_code=404, detail="Person or thumbnail not found")

        gumnut_response = client.faces.download_thumbnail(
            gumnut_person.thumbnail_face_id
        )

        # Get the content and headers from the Gumnut response
        content = gumnut_response.read()
        content_type = gumnut_response.headers.get(
            "content-type", "application/octet-stream"
        )

        # Extract filename from content-disposition header if available
        content_disposition = gumnut_response.headers.get("content-disposition", "")
        filename = None
        if 'filename="' in content_disposition:
            filename = content_disposition.split('filename="')[1].split('"')[0]

        # Build response headers
        response_headers = {
            "Content-Type": content_type,
        }
        if filename:
            response_headers["Content-Disposition"] = f'inline; filename="{filename}"'

        return Response(
            content=content,
            media_type=content_type,
            headers=response_headers,
        )

    except HTTPException:
        # Re-raise HTTP exceptions (like 404 for no thumbnail)
        raise
    except Exception as e:
        # log the error
        logger.warning(f"Error fetching thumbnail for person {id}: {e}")
        raise map_gumnut_error(e, "Failed to fetch asset")


@router.get("/{id}")
async def get_person(
    id: UUID,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> PersonResponseDto:
    """
    Get details for a specific person.
    """
    try:
        gumnut_person = client.people.retrieve(uuid_to_gumnut_person_id(id))

        if not gumnut_person:
            raise HTTPException(status_code=404, detail="Person not found")

        return convert_gumnut_person_to_immich(gumnut_person)

    except HTTPException:
        # Re-raise HTTP exceptions (like 404 for person not found)
        raise
    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch person")


@router.get("/{id}/statistics")
async def get_person_statistics(
    id: UUID,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> PersonStatisticsResponseDto:
    """
    Get asset statistics for a specific person.
    """
    try:
        gumnut_assets = client.assets.list(person_id=uuid_to_gumnut_person_id(id))

        if not gumnut_assets:
            return PersonStatisticsResponseDto(assets=0)
        else:
            return PersonStatisticsResponseDto(assets=len(list(gumnut_assets)))

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch person")


@router.delete("/{id}", status_code=204)
async def delete_person(
    id: UUID,
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> Response:
    """
    Delete a person by their id.
    """
    try:
        client.people.delete(uuid_to_gumnut_person_id(id))

        return Response(status_code=204)

    except Exception as e:
        raise map_gumnut_error(e, "Failed to delete people")


@router.post("/{id}/merge")
async def merge_person(id: UUID, request: MergePersonDto) -> List[BulkIdResponseDto]:
    """
    Merge a person with one or more other people.
    Not supported by Gumnut, so this is a stub implementation that returns an empty list.
    """

    return []


@router.put("/{id}/reassign")
async def reassign_faces(
    id: UUID, request: AssetFaceUpdateDto
) -> List[PersonResponseDto]:
    """
    Reassign faces to a person.
    Returns an empty list.
    """
    # XXX understand how Gumnut and Immich handle face objects
    return []
