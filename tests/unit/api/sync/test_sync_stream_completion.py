from datetime import UTC, datetime

import pytest

from routers.api.sync.stream import generate_sync_stream
from routers.immich_models import SyncRequestType, SyncStreamDto
from tests.unit.api.sync.conftest import create_mock_gumnut_client, create_mock_user


@pytest.mark.anyio
@pytest.mark.parametrize("fail", [False, True])
async def test_only_sync_complete_sets_completion_state(fail):
    user = create_mock_user(datetime.now(UTC))
    client = create_mock_gumnut_client(user)
    if fail:
        client.events.get.side_effect = ValueError("upstream failed")
    state = {}
    chunks = [
        chunk
        async for chunk in generate_sync_stream(
            client,
            SyncStreamDto(types=[SyncRequestType.AssetsV1], reset=False),
            {},
            user,
            completion_state=state,
        )
    ]
    assert bool(state.get("sync_stream_complete")) is not fail
    assert any('"SyncCompleteV1"' in chunk for chunk in chunks) is not fail
