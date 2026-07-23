"""Tests for tags.py endpoints.

The Gumnut API has no tags; the adapter emulates them so Immich clients (notably
immich-go's tagged import) don't fail. `PUT /api/tags` mints a deterministic
synthetic id per requested name and records `id -> value`; `PUT
/api/tags/{id}/assets` recovers the value and appends it to each asset's
description. These tests exercise create, idempotent upsert, assignment,
idempotent re-assignment, inaccessible assets, unknown tag id, and malformed
requests.
"""

from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from routers.api.tags import (
    _append_tag_to_description,
    tag_assets,
    upsert_tags,
)
from routers.immich_models import BulkIdErrorReason, BulkIdsDto, TagUpsertDto
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id
from services.tag_store import (
    TAG_TTL_SECONDS,
    deterministic_tag_id,
    lookup_tag_value,
    remember_tag,
)


def _mock_asset(uid: UUID, description: str | None) -> Mock:
    """Build a mock Gumnut asset carrying only what tag_assets reads."""
    asset = Mock()
    asset.id = uuid_to_gumnut_asset_id(uid)
    asset.metadata = Mock(description=description)
    return asset


def _mock_read(assets_by_uuid: dict[UUID, str | None]) -> AsyncMock:
    """Mock `client.assets.list` returning the given description per asset."""
    from tests.conftest import MockSyncCursorPage

    page = MockSyncCursorPage(
        [_mock_asset(uid, desc) for uid, desc in assets_by_uuid.items()]
    )
    return AsyncMock(return_value=page)


def _change_by_id(mock_call: AsyncMock) -> dict[str, Any]:
    """Map gumnut id → change from a single bulk_update_assets call."""
    mock_call.assert_awaited_once()
    call = mock_call.await_args
    assert call is not None
    return {item["id"]: item["change"] for item in call.kwargs["updates"]}


class TestAppendTagToDescription:
    """The idempotent description-append helper."""

    def test_appends_to_empty_description(self):
        assert _append_tag_to_description(None, "Vacation") == "#Vacation"
        assert _append_tag_to_description("", "Vacation") == "#Vacation"

    def test_appends_as_new_line_to_existing(self):
        assert (
            _append_tag_to_description("A sunset", "Vacation") == "A sunset\n#Vacation"
        )

    def test_idempotent_when_already_present(self):
        existing = "A sunset\n#Vacation"
        assert _append_tag_to_description(existing, "Vacation") == existing

    def test_hierarchical_value_with_spaces_is_preserved(self):
        assert (
            _append_tag_to_description(None, "My Trips/Summer 2026")
            == "#My Trips/Summer 2026"
        )

    def test_distinct_tags_both_appended(self):
        desc = _append_tag_to_description(None, "Trees")
        desc = _append_tag_to_description(desc, "TreesTall")
        assert desc == "#Trees\n#TreesTall"
        # Re-adding the prefix tag stays idempotent (line-equality, not substring).
        assert _append_tag_to_description(desc, "Trees") == desc


class TestDeterministicTagId:
    """Synthetic tag ids are stable and user-scoped."""

    def test_same_inputs_same_id(self):
        assert deterministic_tag_id("user-1", "Vacation") == deterministic_tag_id(
            "user-1", "Vacation"
        )

    def test_different_user_different_id(self):
        assert deterministic_tag_id("user-1", "Vacation") != deterministic_tag_id(
            "user-2", "Vacation"
        )

    def test_different_value_different_id(self):
        assert deterministic_tag_id("user-1", "Vacation") != deterministic_tag_id(
            "user-1", "Work"
        )


class TestUpsertTags:
    """PUT /api/tags."""

    @pytest.mark.anyio
    async def test_creates_tag_returns_nonempty(self):
        user_id = uuid4()
        request = TagUpsertDto(tags=["Vacation"])
        with patch("routers.api.tags.remember_tag", new=AsyncMock()) as mock_remember:
            result = await upsert_tags(request, current_user_id=user_id)

        assert len(result) == 1
        tag = result[0]
        assert tag.name == "Vacation"
        assert tag.value == "Vacation"
        assert tag.id == deterministic_tag_id(str(user_id), "Vacation")
        mock_remember.assert_awaited_once_with(str(user_id), tag.id, "Vacation")

    @pytest.mark.anyio
    async def test_repeated_upsert_is_idempotent(self):
        user_id = uuid4()
        request = TagUpsertDto(tags=["Vacation"])
        with patch("routers.api.tags.remember_tag", new=AsyncMock()):
            first = await upsert_tags(request, current_user_id=user_id)
            second = await upsert_tags(request, current_user_id=user_id)

        assert first[0].id == second[0].id

    @pytest.mark.anyio
    async def test_hierarchical_name_and_order_preserved(self):
        user_id = uuid4()
        request = TagUpsertDto(tags=["Nature/Trees", "Vacation"])
        with patch("routers.api.tags.remember_tag", new=AsyncMock()):
            result = await upsert_tags(request, current_user_id=user_id)

        assert [t.value for t in result] == ["Nature/Trees", "Vacation"]
        # value is the full path; name is the leaf segment.
        assert result[0].name == "Trees"
        assert result[1].name == "Vacation"

    @pytest.mark.anyio
    async def test_newlines_in_value_are_sanitized(self):
        """A tag is stored as one description line, so newlines are stripped at
        the input boundary to keep the append idempotent."""
        user_id = uuid4()
        request = TagUpsertDto(tags=["Bad\nTag\r\nName"])
        with patch("routers.api.tags.remember_tag", new=AsyncMock()) as mock_remember:
            result = await upsert_tags(request, current_user_id=user_id)

        assert "\n" not in result[0].value
        assert "\r" not in result[0].value
        # The sanitized value is what gets recorded for later assignment.
        mock_remember.assert_awaited_once_with(
            str(user_id), result[0].id, result[0].value
        )


class TestTagAssets:
    """PUT /api/tags/{id}/assets."""

    @pytest.mark.anyio
    async def test_appends_tag_to_asset_descriptions(self):
        user_id = uuid4()
        tag_id = uuid4()
        a1, a2 = uuid4(), uuid4()
        mock_client = Mock()
        mock_client.assets.list = _mock_read({a1: "Sunset", a2: None})
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        request = BulkIdsDto(ids=[a1, a2])

        with patch(
            "routers.api.tags.lookup_tag_value",
            new=AsyncMock(return_value="Vacation"),
        ):
            result = await tag_assets(
                tag_id, request, client=mock_client, current_user_id=user_id
            )

        assert [(r.id, r.success) for r in result] == [(a1, True), (a2, True)]
        changes = _change_by_id(mock_client.assets.bulk_update_assets)
        assert changes[uuid_to_gumnut_asset_id(a1)] == {
            "description": "Sunset\n#Vacation"
        }
        assert changes[uuid_to_gumnut_asset_id(a2)] == {"description": "#Vacation"}

    @pytest.mark.anyio
    async def test_reassign_is_idempotent_no_write(self):
        user_id = uuid4()
        tag_id = uuid4()
        a1 = uuid4()
        mock_client = Mock()
        mock_client.assets.list = _mock_read({a1: "Sunset\n#Vacation"})
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        request = BulkIdsDto(ids=[a1])

        with patch(
            "routers.api.tags.lookup_tag_value",
            new=AsyncMock(return_value="Vacation"),
        ):
            result = await tag_assets(
                tag_id, request, client=mock_client, current_user_id=user_id
            )

        assert [(r.id, r.success) for r in result] == [(a1, True)]
        # Already tagged → success, but nothing written.
        mock_client.assets.bulk_update_assets.assert_not_awaited()

    @pytest.mark.anyio
    async def test_inaccessible_assets_marked_not_found(self):
        user_id = uuid4()
        tag_id = uuid4()
        accessible, inaccessible = uuid4(), uuid4()
        mock_client = Mock()
        # The scoped read returns only the accessible asset.
        mock_client.assets.list = _mock_read({accessible: None})
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        request = BulkIdsDto(ids=[accessible, inaccessible])

        with patch(
            "routers.api.tags.lookup_tag_value",
            new=AsyncMock(return_value="Vacation"),
        ):
            result = await tag_assets(
                tag_id, request, client=mock_client, current_user_id=user_id
            )

        by_id = {r.id: r for r in result}
        assert by_id[accessible].success is True
        assert by_id[inaccessible].success is False
        assert by_id[inaccessible].error == BulkIdErrorReason.not_found
        # Only the accessible asset is written.
        changes = _change_by_id(mock_client.assets.bulk_update_assets)
        assert list(changes) == [uuid_to_gumnut_asset_id(accessible)]

    @pytest.mark.anyio
    async def test_unknown_tag_id_is_400(self):
        user_id = uuid4()
        tag_id = uuid4()
        mock_client = Mock()
        mock_client.assets.list = AsyncMock()
        request = BulkIdsDto(ids=[uuid4()])

        with patch(
            "routers.api.tags.lookup_tag_value", new=AsyncMock(return_value=None)
        ):
            with pytest.raises(HTTPException) as exc_info:
                await tag_assets(
                    tag_id, request, client=mock_client, current_user_id=user_id
                )

        assert exc_info.value.status_code == 400
        mock_client.assets.list.assert_not_awaited()

    @pytest.mark.anyio
    async def test_empty_ids_returns_empty(self):
        user_id = uuid4()
        tag_id = uuid4()
        mock_client = Mock()
        mock_client.assets.list = AsyncMock()
        request = BulkIdsDto(ids=[])

        with patch(
            "routers.api.tags.lookup_tag_value",
            new=AsyncMock(return_value="Vacation"),
        ):
            result = await tag_assets(
                tag_id, request, client=mock_client, current_user_id=user_id
            )

        assert result == []
        mock_client.assets.list.assert_not_awaited()

    @pytest.mark.anyio
    async def test_asset_with_null_metadata_is_tagged(self):
        """An asset whose metadata object is absent gets the tag as its whole
        description (the `asset.metadata is None` branch)."""
        user_id = uuid4()
        tag_id = uuid4()
        a1 = uuid4()
        from tests.conftest import MockSyncCursorPage

        asset = Mock()
        asset.id = uuid_to_gumnut_asset_id(a1)
        asset.metadata = None
        mock_client = Mock()
        mock_client.assets.list = AsyncMock(return_value=MockSyncCursorPage([asset]))
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        request = BulkIdsDto(ids=[a1])

        with patch(
            "routers.api.tags.lookup_tag_value",
            new=AsyncMock(return_value="Vacation"),
        ):
            result = await tag_assets(
                tag_id, request, client=mock_client, current_user_id=user_id
            )

        assert [(r.id, r.success) for r in result] == [(a1, True)]
        changes = _change_by_id(mock_client.assets.bulk_update_assets)
        assert changes[uuid_to_gumnut_asset_id(a1)] == {"description": "#Vacation"}

    @pytest.mark.anyio
    async def test_multiple_chunks_accumulate_across_batches(self):
        """Ids spanning more than one chunk each get read + written, and the
        per-id results accumulate across the batched loop."""
        from tests.conftest import MockSyncCursorPage

        user_id = uuid4()
        tag_id = uuid4()
        a1, a2, a3 = uuid4(), uuid4(), uuid4()
        mock_client = Mock()
        # One asset per chunk (a3 is inaccessible: its chunk read returns empty).
        mock_client.assets.list = AsyncMock(
            side_effect=[
                MockSyncCursorPage([_mock_asset(a1, "One")]),
                MockSyncCursorPage([_mock_asset(a2, None)]),
                MockSyncCursorPage([]),
            ]
        )
        mock_client.assets.bulk_update_assets = AsyncMock(return_value=None)
        request = BulkIdsDto(ids=[a1, a2, a3])

        with (
            patch("routers.api.tags.GUMNUT_API_MAX_BULK_IDS", 1),
            patch(
                "routers.api.tags.lookup_tag_value",
                new=AsyncMock(return_value="Vacation"),
            ),
        ):
            result = await tag_assets(
                tag_id, request, client=mock_client, current_user_id=user_id
            )

        # Order preserved across chunks; a3 inaccessible → not_found.
        assert [(r.id, r.success) for r in result] == [
            (a1, True),
            (a2, True),
            (a3, False),
        ]
        assert result[2].error == BulkIdErrorReason.not_found
        # One read per chunk; one write per chunk that had an accessible asset.
        assert mock_client.assets.list.await_count == 3
        assert mock_client.assets.bulk_update_assets.await_count == 2


class TestTagStore:
    """The Redis-backed id -> value store (services/tag_store.py)."""

    @pytest.mark.anyio
    async def test_remember_tag_sets_scoped_key_with_ttl(self):
        tag_id = deterministic_tag_id("user-1", "Vacation")
        redis = Mock()
        redis.set = AsyncMock(return_value=None)
        with patch(
            "services.tag_store.get_redis_client",
            new=AsyncMock(return_value=redis),
        ):
            await remember_tag("user-1", tag_id, "Vacation")

        redis.set.assert_awaited_once_with(
            f"immich_adapter:tag:user-1:{tag_id}", "Vacation", ex=TAG_TTL_SECONDS
        )

    @pytest.mark.anyio
    async def test_lookup_tag_value_round_trips(self):
        tag_id = deterministic_tag_id("user-1", "Vacation")
        redis = Mock()
        redis.get = AsyncMock(return_value="Vacation")
        with patch(
            "services.tag_store.get_redis_client",
            new=AsyncMock(return_value=redis),
        ):
            value = await lookup_tag_value("user-1", tag_id)

        assert value == "Vacation"
        redis.get.assert_awaited_once_with(f"immich_adapter:tag:user-1:{tag_id}")

    @pytest.mark.anyio
    async def test_lookup_tag_value_missing_returns_none(self):
        redis = Mock()
        redis.get = AsyncMock(return_value=None)
        with patch(
            "services.tag_store.get_redis_client",
            new=AsyncMock(return_value=redis),
        ):
            assert await lookup_tag_value("user-1", uuid4()) is None

    @pytest.mark.anyio
    async def test_lookup_tag_value_decodes_bytes(self):
        """decode_responses is on in production, but narrow the broad redis
        return type by decoding a bytes value defensively."""
        redis = Mock()
        redis.get = AsyncMock(return_value=b"Vacation")
        with patch(
            "services.tag_store.get_redis_client",
            new=AsyncMock(return_value=redis),
        ):
            assert await lookup_tag_value("user-1", uuid4()) == "Vacation"


class TestMalformedRequests:
    """Malformed bodies are rejected at the DTO layer (FastAPI → 422)."""

    def test_upsert_requires_tags(self):
        with pytest.raises(ValidationError):
            TagUpsertDto.model_validate({})

    def test_assign_rejects_non_uuid_ids(self):
        with pytest.raises(ValidationError):
            BulkIdsDto.model_validate({"ids": ["not-a-uuid"]})
