import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from gumnut import AsyncGumnut

from routers.immich_models import (
    AssetFaceCreateDto,
    AssetFaceDeleteDto,
    AssetFaceResponseDto,
    FaceDto,
    PersonResponseDto,
    SourceType,
)
from routers.utils.error_mapping import map_gumnut_error
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_face_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_face_id,
    uuid_to_gumnut_person_id,
)
from routers.utils.person_conversion import convert_gumnut_person_to_immich


router = APIRouter(
    prefix="/api/faces",
    tags=["faces"],
    responses={404: {"description": "Not found"}},
)

logger = logging.getLogger(__name__)


@router.delete("/{id}", status_code=204)
async def delete_face(
    id: UUID,
    request: AssetFaceDeleteDto | None = Body(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """Deletes a specific face by ID."""
    try:
        gumnut_face_id = uuid_to_gumnut_face_id(id)
        await client.faces.delete(gumnut_face_id)
    except Exception as e:
        raise map_gumnut_error(e, "Failed to delete face") from e


@router.put("/{id}", response_model=PersonResponseDto)
async def reassign_faces_by_id(
    id: UUID,
    request: FaceDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """Reassigns a face to a different person."""
    try:
        gumnut_face_id = uuid_to_gumnut_face_id(id)
        gumnut_person_id = uuid_to_gumnut_person_id(request.id)
        await client.faces.update(gumnut_face_id, person_id=gumnut_person_id)
        gumnut_person = await client.people.retrieve(gumnut_person_id)
        return convert_gumnut_person_to_immich(gumnut_person)
    except Exception as e:
        raise map_gumnut_error(e, "Failed to reassign face") from e


@router.get("")
async def get_faces(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[AssetFaceResponseDto]:
    """Get all faces detected in an asset."""
    try:
        gumnut_asset_id = uuid_to_gumnut_asset_id(id)

        faces = [f async for f in client.faces.list(asset_id=gumnut_asset_id)]
        if not faces:
            return []

        asset = await client.assets.retrieve(gumnut_asset_id)

        image_width = asset.width or 0
        image_height = asset.height or 0

        # Batch-fetch unique people referenced by faces
        person_ids = {f.person_id for f in faces if f.person_id}
        people_by_id: dict[str, PersonResponseDto] = {}
        for person_id in person_ids:
            try:
                gumnut_person = await client.people.retrieve(person_id)
                people_by_id[person_id] = convert_gumnut_person_to_immich(gumnut_person)
            except Exception:
                logger.warning(
                    "Failed to fetch person for face",
                    extra={"person_id": person_id, "asset_id": gumnut_asset_id},
                )

        result: List[AssetFaceResponseDto] = []
        for face in faces:
            bb = face.bounding_box or {}
            person = people_by_id.get(face.person_id) if face.person_id else None

            result.append(
                AssetFaceResponseDto(
                    id=safe_uuid_from_face_id(face.id),
                    boundingBoxX1=bb.get("x", 0),
                    boundingBoxX2=bb.get("x", 0) + bb.get("w", 0),
                    boundingBoxY1=bb.get("y", 0),
                    boundingBoxY2=bb.get("y", 0) + bb.get("h", 0),
                    imageWidth=image_width,
                    imageHeight=image_height,
                    person=person,
                    sourceType=SourceType.machine_learning,
                )
            )

        return result

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch faces") from e


@router.post("", status_code=201)
async def create_face(
    request: AssetFaceCreateDto,
):
    """
    Create a new face.
    This is a stub implementation that returns a empty response.
    """
    return
