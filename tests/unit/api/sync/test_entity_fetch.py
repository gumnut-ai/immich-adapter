"""Tests for batched sync entity hydration."""

from unittest.mock import Mock

import pytest

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS
from routers.api.sync.entity_fetch import fetch_entities_map
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
