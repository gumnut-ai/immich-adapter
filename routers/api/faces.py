from datetime import date
from typing import List
from uuid import UUID
from fastapi import APIRouter

from routers.immich_models import (
    AssetFaceCreateDto,
    AssetFaceDeleteDto,
    AssetFaceResponseDto,
    FaceDto,
    PersonResponseDto,
)


router = APIRouter(
    prefix="/api/faces",
    tags=["faces"],
    responses={404: {"description": "Not found"}},
)


fake_person_response: PersonResponseDto = PersonResponseDto(
    birthDate=date(1970, 1, 1),
    id="d6773835-4b91-4c7d-8667-26bd5daa1a45",
    isHidden=False,
    name="Ted Mao",
    thumbnailPath="",
)


@router.delete("/{id}", status_code=204)
async def delete_face(
    id: UUID,
    request: AssetFaceDeleteDto,
):
    """
    Deletes a specific face by ID.
    This is a stub implementation that returns a empty response.
    """
    return


@router.put("/{id}", response_model=PersonResponseDto)
async def reassign_faces_by_id(
    id: UUID,
    request: FaceDto,
):
    """
    Reassigns a face to a different person.
    This is a stub implementation that returns a empty response.
    """
    return fake_person_response


@router.get("")
async def get_faces(id: UUID) -> List[AssetFaceResponseDto]:
    """
    Gets a list of all faces.
    This is a stub implementation that returns a empty array.
    """
    return []


@router.post("", status_code=201)
async def create_face(
    request: AssetFaceCreateDto,
):
    """
    Create a new face.
    This is a stub implementation that returns a empty response.
    """
    return
