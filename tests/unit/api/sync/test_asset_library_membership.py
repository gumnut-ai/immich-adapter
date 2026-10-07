"""Asset events reconcile against the session library's current membership.

An asset can move to another Gumnut library and keep its ID. The Gumnut API
records ``asset_moved_out`` in the library it left and ``asset_moved_in`` in
the one it joined, neither naming the other library. Each sync session reads
one library, so for every asset event the adapter asks that library whether it
holds the asset now: present means upsert, absent means remove.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from gumnut import APIConnectionError

from routers.api.sync.stream import generate_sync_stream
from routers.immich_models import SyncRequestType, SyncStreamDto
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id
from tests.unit.api.sync.conftest import (
    TEST_UUID,
    collect_stream,
    create_mock_album_data,
    create_mock_asset_data,
    create_mock_entity_page,
    create_mock_event,
    create_mock_events_response,
    create_mock_gumnut_client,
    create_mock_user,
)

NOW = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
ASSET_ID = uuid_to_gumnut_asset_id(TEST_UUID)


def _asset_event(event_type: str, cursor: str = "cursor_1") -> Mock:
    return create_mock_event(
        entity_type="asset",
        entity_id=ASSET_ID,
        event_type=event_type,
        created_at=NOW,
        cursor=cursor,
    )


def _client(events: list[Mock], *, in_library: bool) -> Mock:
    """A client bound to one library that does or does not hold the asset."""
    client = create_mock_gumnut_client(create_mock_user(NOW))
    client.events.get.return_value = create_mock_events_response(events)
    client.assets.list.return_value = create_mock_entity_page(
        [create_mock_asset_data(NOW)] if in_library else []
    )
    return client


async def _sync(
    client: Mock, request_type: SyncRequestType = SyncRequestType.AssetsV1
) -> list[dict]:
    return await collect_stream(
        generate_sync_stream(
            client, SyncStreamDto(types=[request_type]), {}, create_mock_user(NOW)
        )
    )


def _types(lines: list[dict]) -> list[str]:
    return [line["type"] for line in lines]


@pytest.mark.anyio
async def test_moved_out_asset_is_removed_from_the_library_it_left():
    lines = await _sync(_client([_asset_event("asset_moved_out")], in_library=False))

    assert _types(lines) == ["AssetDeleteV1", "SyncCompleteV1"]
    assert lines[0]["data"] == {"assetId": str(TEST_UUID)}


@pytest.mark.anyio
async def test_moved_in_asset_is_added_to_the_library_it_joined():
    lines = await _sync(_client([_asset_event("asset_moved_in")], in_library=True))

    assert _types(lines) == ["AssetV1", "SyncCompleteV1"]
    assert lines[0]["data"]["id"] == str(TEST_UUID)


@pytest.mark.anyio
async def test_user_of_both_libraries_sees_the_asset_only_where_it_is():
    """The same user syncs the source and the destination as two sessions."""
    source = await _sync(_client([_asset_event("asset_moved_out")], in_library=False))
    destination = await _sync(
        _client([_asset_event("asset_moved_in")], in_library=True)
    )

    assert _types(source) == ["AssetDeleteV1", "SyncCompleteV1"]
    assert _types(destination) == ["AssetV1", "SyncCompleteV1"]


@pytest.mark.anyio
async def test_asset_that_moved_back_before_the_sync_is_kept():
    """A move out is not a tombstone: the asset is back by the time it is read."""
    events = [
        _asset_event("asset_moved_out", "cursor_1"),
        _asset_event("asset_moved_in", "cursor_2"),
    ]

    lines = await _sync(_client(events, in_library=True))

    assert _types(lines) == ["AssetV1", "AssetV1", "SyncCompleteV1"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "event_type",
    [
        "asset_created",
        "asset_updated",
        "asset_trashed",
        "asset_restored",
        "asset_moved_in",
    ],
)
async def test_asset_event_read_after_the_asset_left_removes_it(event_type: str):
    """An older event, or a move in that was followed by a move back out."""
    lines = await _sync(_client([_asset_event(event_type)], in_library=False))

    assert _types(lines) == ["AssetDeleteV1", "SyncCompleteV1"]


@pytest.mark.anyio
@pytest.mark.parametrize("in_library", [True, False])
async def test_duplicate_and_reversed_events_agree_with_membership(in_library: bool):
    events = [
        _asset_event("asset_moved_in", "cursor_1"),
        _asset_event("asset_moved_out", "cursor_2"),
        _asset_event("asset_moved_out", "cursor_3"),
        _asset_event("asset_updated", "cursor_4"),
    ]

    lines = await _sync(_client(events, in_library=in_library))

    expected = "AssetV1" if in_library else "AssetDeleteV1"
    assert _types(lines) == [expected] * 4 + ["SyncCompleteV1"]


@pytest.mark.anyio
async def test_asset_found_again_on_a_later_page_ends_up_present():
    """The removal is emitted in feed order, so the later upsert wins."""
    client = create_mock_gumnut_client(create_mock_user(NOW))
    client.events.get.side_effect = [
        create_mock_events_response(
            [_asset_event("asset_moved_out", "cursor_1")], has_more=True
        ),
        create_mock_events_response([_asset_event("asset_moved_in", "cursor_2")]),
    ]
    client.assets.list.side_effect = [
        create_mock_entity_page([]),
        create_mock_entity_page([create_mock_asset_data(NOW)]),
    ]

    lines = await _sync(client)

    assert _types(lines) == ["AssetDeleteV1", "AssetV1", "SyncCompleteV1"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("request_type", "ack_type"),
    [
        (SyncRequestType.AssetsV1, "AssetV1"),
        (SyncRequestType.AssetsV2, "AssetV2"),
    ],
)
async def test_removal_advances_the_asset_checkpoint(
    request_type: SyncRequestType, ack_type: str
):
    """Otherwise a library emptied by moves would replay them on every sync."""
    client = _client([_asset_event("asset_moved_out", "cursor_9")], in_library=False)

    lines = await _sync(client, request_type)

    assert lines[0]["type"] == "AssetDeleteV1"
    assert lines[0]["ack"].split("|")[:2] == [ack_type, "cursor_9"]


@pytest.mark.anyio
async def test_membership_is_read_in_every_state_by_id():
    client = _client([_asset_event("asset_moved_in")], in_library=True)

    await _sync(client)

    kwargs = client.assets.list.call_args.kwargs
    assert kwargs["state"] == "all"
    assert kwargs["ids"] == [ASSET_ID]


@pytest.mark.anyio
async def test_failed_membership_read_removes_nothing_and_ends_the_stream():
    """A failed read is not absence; with no completion the cursor stays put."""
    client = _client([_asset_event("asset_moved_out")], in_library=False)
    client.assets.list.side_effect = APIConnectionError(request=Mock())

    lines = await _sync(client)

    assert lines == []


OTHER_UUID = UUID("00000000-0000-0000-0000-0000000000aa")
OTHER_ID = uuid_to_gumnut_asset_id(OTHER_UUID)


def _two_asset_client(first_page: Any) -> Mock:
    """Events for two assets; the membership read starts with ``first_page``."""
    events = [
        _asset_event("asset_updated", "cursor_1"),
        create_mock_event(
            entity_type="asset",
            entity_id=OTHER_ID,
            event_type="asset_updated",
            created_at=NOW,
            cursor="cursor_2",
        ),
    ]
    client = _client(events, in_library=False)
    client.assets.list.return_value = first_page
    return client


def _other_asset() -> Mock:
    asset = create_mock_asset_data(NOW)
    asset.id = OTHER_ID
    return asset


@pytest.mark.anyio
async def test_asset_on_a_later_page_of_the_read_is_not_removed():
    first_page = create_mock_entity_page(
        [create_mock_asset_data(NOW)],
        next_page=create_mock_entity_page([_other_asset()]),
    )

    lines = await _sync(_two_asset_client(first_page))

    assert _types(lines) == ["AssetV1", "AssetV1", "SyncCompleteV1"]


@pytest.mark.anyio
async def test_asset_missing_from_every_page_is_removed():
    first_page = create_mock_entity_page(
        [create_mock_asset_data(NOW)], next_page=create_mock_entity_page([])
    )

    lines = await _sync(_two_asset_client(first_page))

    assert _types(lines) == ["AssetV1", "AssetDeleteV1", "SyncCompleteV1"]
    assert lines[1]["data"] == {"assetId": str(OTHER_UUID)}


@pytest.mark.anyio
async def test_failed_later_page_removes_nothing_and_ends_the_stream():
    first_page = create_mock_entity_page(
        [create_mock_asset_data(NOW)], next_page=create_mock_entity_page([])
    )
    first_page.get_next_page = AsyncMock(side_effect=APIConnectionError(request=Mock()))

    lines = await _sync(_two_asset_client(first_page))

    assert lines == []


@pytest.mark.anyio
async def test_read_that_returned_every_asset_fetches_no_further_page():
    """A full page reports more to come even when nothing is left."""
    client = _client([_asset_event("asset_updated")], in_library=True)
    page = create_mock_entity_page(
        [create_mock_asset_data(NOW)], next_page=create_mock_entity_page([])
    )
    page.get_next_page = AsyncMock()
    client.assets.list.return_value = page

    lines = await _sync(client)

    assert _types(lines) == ["AssetV1", "SyncCompleteV1"]
    page.get_next_page.assert_not_called()


@pytest.mark.anyio
async def test_album_cover_survives_an_asset_that_came_back_during_the_sync():
    """An asset read absent, then present, is not treated as deleted later."""
    client = create_mock_gumnut_client(create_mock_user(NOW))
    album = create_mock_album_data(NOW, album_cover_asset_id=ASSET_ID)
    client.events.get.side_effect = [
        create_mock_events_response(
            [_asset_event("asset_moved_out", "cursor_1")], has_more=True
        ),
        create_mock_events_response([_asset_event("asset_moved_in", "cursor_2")]),
        create_mock_events_response(
            [
                create_mock_event(
                    entity_type="album",
                    entity_id=album.id,
                    event_type="album_updated",
                    created_at=NOW,
                    cursor="cursor_3",
                    payload={"album_cover_asset_id": ASSET_ID},
                )
            ]
        ),
    ]
    client.assets.list.side_effect = [
        create_mock_entity_page([]),
        create_mock_entity_page([create_mock_asset_data(NOW)]),
    ]
    client.albums.list.return_value = create_mock_entity_page([album])

    lines = await collect_stream(
        generate_sync_stream(
            client,
            SyncStreamDto(types=[SyncRequestType.AssetsV1, SyncRequestType.AlbumsV1]),
            {},
            create_mock_user(NOW),
        )
    )

    album_line = next(line for line in lines if line["type"] == "AlbumV1")
    assert album_line["data"]["thumbnailAssetId"] == str(TEST_UUID)
