"""Tests for batched sync entity hydration."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from gumnut.types.face_response import FaceResponse

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS
from routers.api.sync.entity_fetch import (
    fetch_entities_map,
    fetch_suppressed_face_ids,
)
from tests.conftest import MockSyncCursorPage


def test_bulk_id_limit_is_200():
    assert GUMNUT_API_MAX_BULK_IDS == 200


@pytest.mark.anyio
@pytest.mark.parametrize(
    "total, expected_sizes",
    [
        (GUMNUT_API_MAX_BULK_IDS, [GUMNUT_API_MAX_BULK_IDS]),
        (
            GUMNUT_API_MAX_BULK_IDS + 1,
            [GUMNUT_API_MAX_BULK_IDS, 1],
        ),
    ],
)
async def test_asset_hydration_chunks_at_bulk_id_limit(
    total: int, expected_sizes: list[int]
):
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([]))
    entity_ids = [f"asset_{index}" for index in range(total)]

    await fetch_entities_map(client, "asset", entity_ids)

    calls = client.assets.list.call_args_list
    assert [len(call.kwargs["ids"]) for call in calls] == expected_sizes
    assert [call.kwargs["limit"] for call in calls] == expected_sizes
    assert [
        entity_id for call in calls for entity_id in call.kwargs["ids"]
    ] == entity_ids


def _face(face_id: str, asset_id: str) -> FaceResponse:
    return FaceResponse(
        id=face_id,
        asset_id=asset_id,
        bounding_box={"x": 1, "y": 1, "w": 2, "h": 2},
        source="automatic",
        created_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
    )


def _asset(asset_id: str, kind: str = "original") -> Mock:
    asset = Mock()
    asset.id = asset_id
    asset.kind = kind
    return asset


@pytest.mark.anyio
async def test_suppressed_face_ids_empty_input_skips_the_read():
    client = Mock()

    assert await fetch_suppressed_face_ids(client, []) == set()
    client.assets.list.assert_not_called()


@pytest.mark.anyio
async def test_suppressed_face_ids_partitions_per_asset_and_fails_safe():
    faces = [
        _face("face_original", "asset_original"),
        _face("face_original_2", "asset_original"),
        _face("face_edited", "asset_edited"),
        _face("face_missing", "asset_missing"),
    ]
    client = Mock()
    client.assets.list = Mock(
        return_value=MockSyncCursorPage(
            [_asset("asset_original"), _asset("asset_edited", kind="edit")]
        )
    )

    suppressed = await fetch_suppressed_face_ids(client, faces)

    assert suppressed == {"face_edited", "face_missing"}
    client.assets.list.assert_called_once()
    kwargs = client.assets.list.call_args.kwargs
    assert kwargs["ids"] == ["asset_original", "asset_edited", "asset_missing"]
    assert kwargs["state"] == "all"
    assert "include" not in kwargs


@pytest.mark.anyio
async def test_suppressed_face_ids_chunk_at_bulk_id_limit():
    total = GUMNUT_API_MAX_BULK_IDS + 1
    faces = [_face(f"face_{index}", f"asset_{index}") for index in range(total)]
    client = Mock()
    client.assets.list = Mock(
        side_effect=lambda **kwargs: MockSyncCursorPage(
            [_asset(asset_id) for asset_id in kwargs["ids"]]
        )
    )

    suppressed = await fetch_suppressed_face_ids(client, faces)

    assert suppressed == set()
    calls = client.assets.list.call_args_list
    assert [len(call.kwargs["ids"]) for call in calls] == [
        GUMNUT_API_MAX_BULK_IDS,
        1,
    ]
    assert [call.kwargs["limit"] for call in calls] == [GUMNUT_API_MAX_BULK_IDS, 1]
