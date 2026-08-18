"""Tests for batch download planning and streamed ZIP archives."""

from io import BytesIO
from typing import cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from gumnut.types.asset_response import AssetResponse

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS
from routers.api.download import (
    _assets_by_ids,
    _assets_for_info,
    _build_download_info,
    _deduplicated_archive_names,
    download_archive,
)
from routers.immich_models import DownloadArchiveDto, DownloadInfoDto
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_user_id,
)
from tests.conftest import MockSyncCursorPage, make_gumnut_asset


def _download_asset(
    asset_id: UUID,
    *,
    filename: str = "photo.jpg",
    size: int = 10,
    url: str | None = None,
) -> AssetResponse:
    asset = make_gumnut_asset(
        asset_id=uuid_to_gumnut_asset_id(asset_id),
        original_file_name=filename,
    )
    asset.file_data.file_size_bytes = size
    asset.asset_urls = (
        {"original": Mock(url=url or f"https://cdn.example.com/{asset_id}")}
        if url is not None or filename
        else {}
    )
    return cast(AssetResponse, asset)


async def _collect_response_body(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


@pytest.mark.anyio
async def test_archive_streams_valid_zip_in_request_order_with_safe_unique_names():
    first_id = uuid4()
    second_id = uuid4()
    assets = [
        _download_asset(
            first_id,
            filename="../trip/photo.jpg",
            size=5,
            url="https://cdn.example.com/first",
        ),
        _download_asset(
            second_id,
            filename="../trip/photo.jpg",
            size=6,
            url="https://cdn.example.com/second",
        ),
    ]
    client = Mock()
    # Deliberately reverse the backend result; the archive must follow request order.
    client.assets.list = Mock(return_value=MockSyncCursorPage(list(reversed(assets))))

    chunks = {
        "https://cdn.example.com/first": (b"first",),
        "https://cdn.example.com/second": (b"sec", b"ond"),
    }

    async def fake_cdn_chunks(url: str):
        for chunk in chunks[url]:
            yield chunk

    with patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks):
        response = await download_archive(
            DownloadArchiveDto(assetIds=[first_id, second_id]), client=client
        )
        archive = await _collect_response_body(response)

    assert response.media_type == "application/zip"
    assert response.headers["content-disposition"] == (
        'attachment; filename="assets.zip"'
    )
    with ZipFile(BytesIO(archive)) as zip_file:
        assert zip_file.namelist() == ["..tripphoto.jpg", "..tripphoto+1.jpg"]
        assert zip_file.read("..tripphoto.jpg") == b"first"
        assert zip_file.read("..tripphoto+1.jpg") == b"second"


@pytest.mark.anyio
async def test_assets_by_ids_chunks_backend_filter_and_preserves_duplicates():
    asset_ids = [uuid4() for _ in range(GUMNUT_API_MAX_BULK_IDS + 1)]
    selected = _download_asset(asset_ids[0])
    client = Mock()
    client.assets.list = Mock(
        side_effect=[
            MockSyncCursorPage([selected]),
            MockSyncCursorPage([]),
        ]
    )

    result = await _assets_by_ids(client, [*asset_ids, asset_ids[0]])

    assert result == [selected, selected]
    assert client.assets.list.call_count == 2
    first_call, second_call = client.assets.list.call_args_list
    assert len(first_call.kwargs["ids"]) == GUMNUT_API_MAX_BULK_IDS
    assert len(second_call.kwargs["ids"]) == 2
    assert first_call.kwargs["include"] == ["file_data", "variants"]


def test_download_info_matches_immich_threshold_grouping():
    assets = [
        _download_asset(uuid4(), size=5_000),
        _download_asset(uuid4(), size=100_000),
        _download_asset(uuid4(), size=23_456),
        _download_asset(uuid4(), size=123_000),
    ]

    result = _build_download_info(assets, 30_000)

    assert result.totalSize == 251_456
    assert [archive.size for archive in result.archives] == [105_000, 146_456]
    assert result.archives[0].assetIds == [
        safe_uuid_from_asset_id(assets[0].id),
        safe_uuid_from_asset_id(assets[1].id),
    ]


@pytest.mark.anyio
async def test_album_info_validates_album_then_pages_its_assets():
    album_id = uuid4()
    asset = _download_asset(uuid4())
    client = Mock()
    client.albums.retrieve = AsyncMock(return_value=Mock())
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))

    result = await _assets_for_info(DownloadInfoDto(albumId=album_id), client)

    assert result == [asset]
    client.albums.retrieve.assert_awaited_once_with(uuid_to_gumnut_album_id(album_id))
    assert client.assets.list.call_args.kwargs["album_id"] == (
        uuid_to_gumnut_album_id(album_id)
    )


@pytest.mark.anyio
async def test_user_info_only_allows_the_authenticated_user():
    client = Mock()
    client.assets.list = Mock()
    current_user_id = uuid4()
    client.users.me = AsyncMock(
        return_value=Mock(id=uuid_to_gumnut_user_id(current_user_id))
    )

    with pytest.raises(HTTPException) as exc_info:
        await _assets_for_info(DownloadInfoDto(userId=uuid4()), client)

    assert exc_info.value.status_code == 403
    client.assets.list.assert_not_called()


@pytest.mark.anyio
async def test_info_requires_one_selector():
    with pytest.raises(HTTPException) as exc_info:
        await _assets_for_info(DownloadInfoDto(), Mock())

    assert exc_info.value.status_code == 400


def test_archive_names_sanitize_empty_and_deduplicate_extensions():
    assets = [
        _download_asset(uuid4(), filename=".."),
        _download_asset(uuid4(), filename=".."),
        _download_asset(uuid4(), filename=r"folder\photo.tar.gz"),
        _download_asset(uuid4(), filename=r"folder\photo.tar.gz"),
    ]

    assert _deduplicated_archive_names(assets) == [
        "unnamed",
        "unnamed+1",
        "folderphoto.tar.gz",
        "folderphoto.tar+1.gz",
    ]


@pytest.mark.anyio
async def test_info_omits_assets_without_an_original_variant():
    current_user_id = uuid4()
    available = _download_asset(uuid4())
    unavailable = _download_asset(uuid4())
    unavailable.asset_urls = {}
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([available, unavailable]))
    client.users.me = AsyncMock(
        return_value=Mock(id=uuid_to_gumnut_user_id(current_user_id))
    )

    result = await _assets_for_info(DownloadInfoDto(userId=current_user_id), client)

    assert result == [available]
