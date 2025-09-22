from fastapi import APIRouter
from uuid import UUID
from datetime import datetime, timezone
from typing import List

from routers.api.auth import get_current_user_id
from routers.immich_models import (
    CreateLibraryDto,
    LibraryResponseDto,
    LibraryStatsResponseDto,
    UpdateLibraryDto,
    ValidateLibraryDto,
    ValidateLibraryResponseDto,
)


router = APIRouter(
    prefix="/api/libraries",
    tags=["libraries"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def get_all_libraries() -> List[LibraryResponseDto]:
    """
    Get all libraries.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("", status_code=201)
async def create_library(request: CreateLibraryDto) -> LibraryResponseDto:
    """
    Create a library.
    This is a stub implementation that returns a fake library response.
    """
    return LibraryResponseDto(
        id="library-id",
        name=request.name or "New Library",
        ownerId=str(get_current_user_id()),
        assetCount=0,
        importPaths=request.importPaths or [],
        exclusionPatterns=request.exclusionPatterns or [],
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
        refreshedAt=datetime.now(tz=timezone.utc),
    )


@router.get("/{id}")
async def get_library(id: UUID) -> LibraryResponseDto:
    """
    Get library by ID.
    This is a stub implementation that returns a fake library response.
    """
    return LibraryResponseDto(
        id=str(id),
        name="Sample Library",
        ownerId=str(get_current_user_id()),
        assetCount=0,
        importPaths=[],
        exclusionPatterns=[],
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
        refreshedAt=datetime.now(tz=timezone.utc),
    )


@router.put("/{id}")
async def update_library(id: UUID, request: UpdateLibraryDto) -> LibraryResponseDto:
    """
    Update library.
    This is a stub implementation that returns a fake updated library response.
    """
    return LibraryResponseDto(
        id=str(id),
        name=request.name or "Updated Library",
        ownerId=str(get_current_user_id()),
        assetCount=0,
        importPaths=request.importPaths or [],
        exclusionPatterns=request.exclusionPatterns or [],
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
        refreshedAt=datetime.now(tz=timezone.utc),
    )


@router.delete("/{id}", status_code=204)
async def delete_library(id: UUID):
    """
    Delete library.
    This is a stub implementation that does not perform any action.
    """
    return


@router.post("/{id}/scan", status_code=204)
async def scan_library(id: UUID):
    """
    Scan library.
    This is a stub implementation that does not perform any action.
    """
    return


@router.get("/{id}/statistics")
async def get_library_statistics(id: UUID) -> LibraryStatsResponseDto:
    """
    Get library statistics.
    This is a stub implementation that returns zero statistics.
    """
    return LibraryStatsResponseDto(
        photos=0,
        videos=0,
        total=0,
        usage=0,
    )


@router.post("/{id}/validate")
async def validate(id: UUID, request: ValidateLibraryDto) -> ValidateLibraryResponseDto:
    """
    Validate library.
    This is a stub implementation that returns fake validation results.
    """
    return ValidateLibraryResponseDto(
        importPaths=[],
    )
