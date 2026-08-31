"""Unit tests for API key endpoint stubs.

These stubs hand-construct generated DTOs, so a model regeneration that adds
required fields breaks them at runtime (pydantic ValidationError -> 500), not
in pyright. The tests here exercise that construction path.
"""

import pytest

from routers.api.api_keys import create_api_key
from routers.immich_models import ApiKeyCreateDto, ApiKeyCreateResponseDto, Permission


class TestCreateApiKey:
    @pytest.mark.anyio
    async def test_constructs_valid_dto(self):
        response = await create_api_key(
            ApiKeyCreateDto(name="test key", permissions=[Permission.asset_read])
        )

        assert isinstance(response, ApiKeyCreateResponseDto)
        assert response.secret

    @pytest.mark.anyio
    async def test_flat_fields_mirror_deprecated_nested_key(self):
        """v3.2 flattens the key fields; old clients still read the nested
        `apiKey`, so both projections must describe the same key."""
        response = await create_api_key(
            ApiKeyCreateDto(name="test key", permissions=[Permission.asset_read])
        )

        assert response.apiKey is not None
        assert response.id == response.apiKey.id
        assert response.name == response.apiKey.name
        assert response.permissions == response.apiKey.permissions
        assert response.createdAt == response.apiKey.createdAt
        assert response.updatedAt == response.apiKey.updatedAt
