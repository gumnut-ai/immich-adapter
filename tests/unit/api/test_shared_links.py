"""Tests for /shared-links/* Immich v3 shape conformance.

Immich v3 removed the shared-link token/key/slug access surface:
- `SharedLinkResponseDto.token` is gone (`key`/`slug`/`password` retained).
- `GET /shared-links/me` lost its `password`/`token` query params (kept `key`/`slug`).
- `SharedLinkEditDto.changeExpiryTime` is gone.
- `PUT /shared-links/{id}/assets` lost its `key`/`slug` query params.

These guard against re-introducing the removed surface. They assert on
`model_fields` and `inspect.signature` rather than constructing a
`SharedLinkResponseDto`: on the `migration/immichv3` branch the regenerated
DTOs cannot be instantiated (their `pattern`-constrained UUID/datetime fields
raise under the pinned pydantic), but class-level field/signature inspection
does not instantiate anything and runs cleanly.
"""

import inspect

from routers.api.shared_links import add_shared_link_assets, get_my_shared_link
from routers.immich_models import SharedLinkEditDto, SharedLinkResponseDto


def test_shared_link_response_dto_dropped_token():
    """v3 removed SharedLinkResponseDto.token; key/slug remain."""
    assert "token" not in SharedLinkResponseDto.model_fields
    assert "key" in SharedLinkResponseDto.model_fields
    assert "slug" in SharedLinkResponseDto.model_fields


def test_shared_link_edit_dto_dropped_change_expiry_time():
    """v3 removed SharedLinkEditDto.changeExpiryTime."""
    assert "changeExpiryTime" not in SharedLinkEditDto.model_fields


def test_get_my_shared_link_dropped_password_and_token_params():
    """GET /shared-links/me lost password/token query params but kept key/slug."""
    params = set(inspect.signature(get_my_shared_link).parameters)
    assert "password" not in params
    assert "token" not in params
    assert {"key", "slug"} <= params


def test_add_shared_link_assets_dropped_key_and_slug_params():
    """PUT /shared-links/{id}/assets lost its key/slug query params."""
    params = set(inspect.signature(add_shared_link_assets).parameters)
    assert "key" not in params
    assert "slug" not in params
