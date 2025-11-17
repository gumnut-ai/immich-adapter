from datetime import datetime, timezone
from typing import List
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from routers.immich_models import (
    AssetIdsDto,
    AssetIdsResponseDto,
    SharedLinkCreateDto,
    SharedLinkEditDto,
    SharedLinkResponseDto,
    SharedLinkType,
)
from routers.utils.current_user import get_current_user_id


router = APIRouter(
    prefix="/api/shared-links",
    tags=["shared-links"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def get_all_shared_links(
    albumId: UUID = Query(default=None),
) -> List[SharedLinkResponseDto]:
    """
    Get all shared links
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("", status_code=201)
async def create_shared_link(
    request: SharedLinkCreateDto,
    current_user_id: UUID = Depends(get_current_user_id),
) -> SharedLinkResponseDto:
    """
    Create a shared link
    This is a stub implementation that returns a fake shared link response.
    """
    now = datetime.now(timezone.utc)
    return SharedLinkResponseDto(
        album=None,
        allowDownload=True,
        allowUpload=False,
        assets=[],
        createdAt=now,
        description="Shared link",
        expiresAt=now,
        id=str(uuid4()),
        key="dummy-key",
        password="",
        showMetadata=True,
        slug="dummy-slug",
        token="dummy-token",
        type=SharedLinkType.INDIVIDUAL,
        userId=str(current_user_id),
    )


@router.get("/me")
async def get_my_shared_link(
    password: str = Query(default=None),
    token: str = Query(default=None),
    key: str = Query(default=None),
    slug: str = Query(default=None),
    current_user_id: UUID = Depends(get_current_user_id),
) -> SharedLinkResponseDto:
    """
    Get my shared link
    This is a stub implementation that returns a fake shared link response.
    """
    now = datetime.now(timezone.utc)
    return SharedLinkResponseDto(
        album=None,
        allowDownload=True,
        allowUpload=False,
        assets=[],
        createdAt=now,
        description="My shared link",
        expiresAt=now,
        id=str(current_user_id),
        key=key or "dummy-key",
        password="",
        showMetadata=True,
        slug=slug or "dummy-slug",
        token=token or "dummy-token",
        type=SharedLinkType.INDIVIDUAL,
        userId=str(current_user_id),
    )


@router.get("/{id}")
async def get_shared_link_by_id(
    id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
) -> SharedLinkResponseDto:
    """
    Get a shared link by ID
    This is a stub implementation that returns a fake shared link response.
    """
    now = datetime.now(timezone.utc)
    return SharedLinkResponseDto(
        album=None,
        allowDownload=True,
        allowUpload=False,
        assets=[],
        createdAt=now,
        description="Shared link by ID",
        expiresAt=now,
        id=str(id),
        key="dummy-key",
        password="",
        showMetadata=True,
        slug="dummy-slug",
        token="dummy-token",
        type=SharedLinkType.INDIVIDUAL,
        userId=str(current_user_id),
    )


@router.patch("/{id}")
async def update_shared_link(
    id: UUID,
    request: SharedLinkEditDto,
    current_user_id: UUID = Depends(get_current_user_id),
) -> SharedLinkResponseDto:
    """
    Update a shared link
    This is a stub implementation that returns a fake shared link response.
    """
    now = datetime.now(timezone.utc)
    return SharedLinkResponseDto(
        album=None,
        allowDownload=True,
        allowUpload=False,
        assets=[],
        createdAt=now,
        description="Updated shared link",
        expiresAt=now,
        id=str(id),
        key="dummy-key",
        password="",
        showMetadata=True,
        slug="dummy-slug",
        token="dummy-token",
        type=SharedLinkType.INDIVIDUAL,
        userId=str(current_user_id),
    )


@router.delete("/{id}", status_code=204)
async def remove_shared_link(id: UUID):
    """
    Remove a shared link
    This is a stub implementation that does not perform any action.
    """
    return


@router.put("/{id}/assets")
async def add_shared_link_assets(
    id: UUID,
    request: AssetIdsDto,
    key: str = Query(default=None),
    slug: str = Query(default=None),
) -> List[AssetIdsResponseDto]:
    """
    Add assets to a shared link
    This is a stub implementation that returns an empty list.
    """
    return []


@router.delete("/{id}/assets")
async def remove_shared_link_assets(
    id: UUID,
    request: AssetIdsDto,
    key: str = Query(default=None),
    slug: str = Query(default=None),
) -> List[AssetIdsResponseDto]:
    """
    Remove assets from a shared link
    This is a stub implementation that returns an empty list.
    """
    return []
