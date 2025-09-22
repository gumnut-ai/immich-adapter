from datetime import datetime
from typing import List
from uuid import UUID
from zoneinfo import ZoneInfo
from fastapi import APIRouter

from routers.immich_models import (
    PartnerCreateDto,
    PartnerDirection,
    PartnerResponseDto,
    PartnerUpdateDto,
    UserAvatarColor,
)

router = APIRouter(
    prefix="/api/partners",
    tags=["partners"],
    responses={404: {"description": "Not found"}},
)


fake_partner: PartnerResponseDto = PartnerResponseDto(
    avatarColor=UserAvatarColor.primary,
    email="partner@immich.test",
    id="d6773835-4b91-4c7d-8667-26bd5daa1a45",
    name="Fake Partner",
    profileChangedAt=datetime.now(tz=ZoneInfo("Etc/UTC")),
    profileImagePath="",
)


@router.get("")
async def get_partners(
    direction: PartnerDirection,
) -> List[PartnerResponseDto]:
    """
    Return a list of partners.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("", status_code=201)
async def create_partners(
    request: PartnerCreateDto,
) -> PartnerResponseDto:
    """
    Create a new partner.
    This is a stub implementation that returns a dummy partner.
    """
    return fake_partner


@router.post("/{id}", status_code=201)
async def create_partner_eprecated(id: UUID) -> PartnerResponseDto:
    """
    Delete a partner.
    This is a stub implementation that returns a dummy partner.
    """
    return fake_partner


@router.put("/{id}")
async def update_partner(id: UUID, request: PartnerUpdateDto) -> PartnerResponseDto:
    """
    Update a partner.
    This is a stub implementation that returns a dummy partner.
    """
    return fake_partner


@router.delete("/{id}", status_code=204)
async def remove_partner(id: UUID):
    """
    Delete a partner.
    This is a stub implementation that returns nothing.
    """
