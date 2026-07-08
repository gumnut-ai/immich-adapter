from fastapi import APIRouter
from uuid import UUID, uuid4
from datetime import datetime, timezone
from typing import List

from routers.immich_models import (
    ApiKeyCreateDto,
    ApiKeyResponseDto,
    ApiKeyUpdateDto,
    ApiKeyCreateResponseDto,
    Permission,
)


router = APIRouter(
    prefix="/api/api-keys",
    tags=["api-keys"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def get_api_keys() -> List[ApiKeyResponseDto]:
    """
    Get all API keys.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("", status_code=201)
async def create_api_key(request: ApiKeyCreateDto) -> ApiKeyCreateResponseDto:
    """
    Create a new API key.
    This is a stub implementation that returns a fake API key response.
    """
    api_key = ApiKeyResponseDto(
        id=uuid4(),
        name=request.name or "API Key",
        permissions=request.permissions,
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
    )
    return ApiKeyCreateResponseDto(apiKey=api_key, secret="fake-secret-key-12345")


@router.get("/me")
async def get_my_api_key() -> ApiKeyResponseDto:
    """
    Get current API key.
    This is a stub implementation that returns a fake API key response.
    """
    return ApiKeyResponseDto(
        id=uuid4(),
        name="Current API Key",
        permissions=[Permission.asset_read],
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.get("/{id}")
async def get_api_key(id: UUID) -> ApiKeyResponseDto:
    """
    Get API key by ID.
    This is a stub implementation that returns a fake API key response.
    """
    return ApiKeyResponseDto(
        id=str(id),
        name="API Key",
        permissions=[Permission.asset_read],
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.put("/{id}")
async def update_api_key(id: UUID, request: ApiKeyUpdateDto) -> ApiKeyResponseDto:
    """
    Update API key.
    This is a stub implementation that returns a fake updated API key response.
    """
    return ApiKeyResponseDto(
        id=str(id),
        name=request.name or "Updated API Key",
        permissions=request.permissions or [Permission.asset_read],
        createdAt=datetime.now(tz=timezone.utc),
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.delete("/{id}", status_code=204)
async def delete_api_key(id: UUID):
    """
    Delete API key.
    This is a stub implementation that does not perform any action.
    """
    return
