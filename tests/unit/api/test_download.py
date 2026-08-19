"""Tests for batch download planning and streamed ZIP archives."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import datetime
from io import BytesIO
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4
from zipfile import ZipFile

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from gumnut.types.asset_response import AssetResponse

from routers.api.constants import GUMNUT_API_MAX_BULK_IDS
from routers.api.download import (
    _ArchiveAsset,
    _assets_by_ids,
    _assets_for_info,
    _build_download_info,
    _deduplicated_archive_name,
    _stream_archive,
    download_archive,
    get_download_info,
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
    asset.asset_urls = {
        "original": Mock(url=url or f"https://cdn.example.com/{asset_id}")
    }
    return cast(AssetResponse, asset)


async def _collect_response_body(response: StreamingResponse) -> bytes:
    return b"".join([cast(bytes, chunk) async for chunk in response.body_iterator])


def _archive_names(filenames: list[str]) -> list[str]:
    next_suffix: dict[str, int] = {}
    emitted: set[str] = set()
    return [
        _deduplicated_archive_name(filename, next_suffix, emitted)
        for filename in filenames
    ]


def _compact_archive_asset(asset: AssetResponse) -> _ArchiveAsset:
    assert asset.file_data is not None
    assert asset.asset_urls is not None
    return _ArchiveAsset(
        filename=asset.original_file_name,
        modified_at=asset.file_data.file_modified_at,
        size=asset.file_data.file_size_bytes,
        url=asset.asset_urls["original"].url,
    )


@pytest.mark.anyio
async def test_archive_streams_valid_zip_in_request_order_with_safe_unique_names() -> (
    None
):
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
    client.assets.list = Mock(return_value=MockSyncCursorPage(list(reversed(assets))))
    chunks = {
        "https://cdn.example.com/first": (b"first",),
        "https://cdn.example.com/second": (b"sec", b"ond"),
    }

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
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
async def test_assets_by_ids_chunks_backend_filter_and_preserves_duplicates() -> None:
    asset_ids = [uuid4() for _ in range(GUMNUT_API_MAX_BULK_IDS + 1)]
    selected = _download_asset(asset_ids[0])
    client = Mock()
    client.assets.list = Mock(
        side_effect=[MockSyncCursorPage([selected]), MockSyncCursorPage([selected])]
    )

    result = [
        asset
        async for asset in _assets_by_ids(
            client,
            [*asset_ids, asset_ids[0]],
            include=["file_data", "variants"],
        )
    ]

    assert result == [selected, selected]
    assert client.assets.list.call_count == 2
    first_call, second_call = client.assets.list.call_args_list
    assert len(first_call.kwargs["ids"]) == GUMNUT_API_MAX_BULK_IDS
    assert len(second_call.kwargs["ids"]) == 2
    assert first_call.kwargs["include"] == ["file_data", "variants"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("asset_count", "expected_chunk_lengths"),
    [
        (GUMNUT_API_MAX_BULK_IDS, [GUMNUT_API_MAX_BULK_IDS]),
        (GUMNUT_API_MAX_BULK_IDS + 1, [GUMNUT_API_MAX_BULK_IDS, 1]),
    ],
)
async def test_assets_by_ids_pins_exact_bulk_boundaries(
    asset_count: int, expected_chunk_lengths: list[int]
) -> None:
    asset_ids = [uuid4() for _ in range(asset_count)]
    client = Mock()
    client.assets.list = Mock(
        side_effect=[MockSyncCursorPage([]) for _ in expected_chunk_lengths]
    )

    result = [
        asset
        async for asset in _assets_by_ids(client, asset_ids, include=["file_data"])
    ]

    assert result == []
    assert [
        len(call.kwargs["ids"]) for call in client.assets.list.call_args_list
    ] == expected_chunk_lengths


@pytest.mark.anyio
async def test_download_info_matches_immich_threshold_grouping() -> None:
    assets = [
        _download_asset(uuid4(), size=5_000),
        _download_asset(uuid4(), size=100_000),
        _download_asset(uuid4(), size=23_456),
        _download_asset(uuid4(), size=123_000),
    ]

    async def asset_stream() -> AsyncIterator[AssetResponse]:
        for asset in assets:
            yield asset

    result = await _build_download_info(asset_stream(), 30_000)

    assert result.totalSize == 251_456
    assert [archive.size for archive in result.archives] == [105_000, 146_456]
    assert result.archives[0].assetIds == [
        safe_uuid_from_asset_id(assets[0].id),
        safe_uuid_from_asset_id(assets[1].id),
    ]


@pytest.mark.anyio
async def test_album_info_validates_album_then_pages_its_assets() -> None:
    album_id = uuid4()
    asset = _download_asset(uuid4())
    client = Mock()
    client.albums.retrieve = AsyncMock(return_value=Mock())
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))

    result = [
        item
        async for item in _assets_for_info(DownloadInfoDto(albumId=album_id), client)
    ]

    assert result == [asset]
    client.albums.retrieve.assert_awaited_once_with(uuid_to_gumnut_album_id(album_id))
    assert client.assets.list.call_args.kwargs["album_id"] == (
        uuid_to_gumnut_album_id(album_id)
    )


@pytest.mark.anyio
async def test_user_info_only_allows_the_authenticated_user() -> None:
    client = Mock()
    client.assets.list = Mock()
    current_user_id = uuid4()
    client.users.me = AsyncMock(
        return_value=Mock(id=uuid_to_gumnut_user_id(current_user_id))
    )

    with pytest.raises(HTTPException) as exc_info:
        _ = [
            asset
            async for asset in _assets_for_info(DownloadInfoDto(userId=uuid4()), client)
        ]

    assert exc_info.value.status_code == 403
    client.assets.list.assert_not_called()


@pytest.mark.anyio
async def test_info_requires_one_selector() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _ = [asset async for asset in _assets_for_info(DownloadInfoDto(), Mock())]

    assert exc_info.value.status_code == 400


def test_archive_names_sanitize_empty_and_deduplicate_extensions() -> None:
    assert _archive_names(
        ["..", "..", r"folder\photo.tar.gz", r"folder\photo.tar.gz"]
    ) == [
        "unnamed",
        "unnamed+1",
        "folderphoto.tar.gz",
        "folderphoto.tar+1.gz",
    ]


def test_archive_names_avoid_collisions_with_generated_suffixes() -> None:
    assert _archive_names(["a.jpg", "a.jpg", "a+1.jpg"]) == [
        "a.jpg",
        "a+1.jpg",
        "a+1+1.jpg",
    ]


def test_archive_names_avoid_case_insensitive_collisions() -> None:
    assert _archive_names(["Photo.jpg", "photo.jpg", "PHOTO+1.JPG"]) == [
        "Photo.jpg",
        "photo+1.jpg",
        "PHOTO+1+1.JPG",
    ]


def test_archive_names_avoid_windows_trailing_character_collisions() -> None:
    assert _archive_names(["photo.jpg", "photo.jpg ."]) == [
        "photo.jpg",
        "photo+1.jpg",
    ]


def test_archive_names_avoid_canonically_equivalent_unicode_collisions() -> None:
    assert _archive_names(["café.jpg", "café.jpg", "CAFÉ+1.JPG"]) == [
        "café.jpg",
        "café+1.jpg",
        "CAFÉ+1+1.JPG",
    ]


@pytest.mark.anyio
async def test_info_requests_only_file_data_without_signing_original_urls() -> None:
    current_user_id = uuid4()
    assets = [_download_asset(uuid4()), _download_asset(uuid4())]
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage(assets))
    client.users.me = AsyncMock(
        return_value=Mock(id=uuid_to_gumnut_user_id(current_user_id))
    )

    result = [
        asset
        async for asset in _assets_for_info(
            DownloadInfoDto(userId=current_user_id), client
        )
    ]

    assert result == assets
    assert client.assets.list.call_args.kwargs["include"] == ["file_data"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("archive_size", "expected_sizes"),
    [(None, [12]), (10, [11, 1])],
)
async def test_get_download_info_composes_default_and_explicit_thresholds(
    archive_size: int | None, expected_sizes: list[int]
) -> None:
    asset_ids = [uuid4(), uuid4(), uuid4()]
    assets = [
        _download_asset(asset_ids[0], size=10),
        _download_asset(asset_ids[1], size=1),
        _download_asset(asset_ids[2], size=1),
    ]
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage(assets))

    result = await get_download_info(
        DownloadInfoDto(assetIds=asset_ids, archiveSize=archive_size), client=client
    )

    assert result.totalSize == 12
    assert [archive.size for archive in result.archives] == expected_sizes
    assert client.assets.list.call_args.kwargs["include"] == ["file_data"]


@pytest.mark.anyio
async def test_info_rejects_missing_requested_asset() -> None:
    asset_ids = [uuid4(), uuid4()]
    client = Mock()
    client.assets.list = Mock(
        return_value=MockSyncCursorPage([_download_asset(asset_ids[0])])
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_download_info(DownloadInfoDto(assetIds=asset_ids), client=client)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Not found or no asset.download access"


@pytest.mark.anyio
async def test_closing_partial_archive_immediately_closes_active_cdn_response() -> None:
    asset = _download_asset(uuid4(), size=1_000_004)
    response = Mock()
    response.aclose = AsyncMock()
    chunk_sizes: list[int | None] = []

    async def aiter_bytes(chunk_size: int | None = None) -> AsyncIterator[bytes]:
        chunk_sizes.append(chunk_size)
        yield b"x" * 1_000_000
        yield b"rest"

    response.aiter_bytes = aiter_bytes
    archive = _stream_archive([_compact_archive_asset(asset)])

    with patch(
        "routers.api.download.open_cdn_response",
        new_callable=AsyncMock,
        return_value=response,
    ) as open_response:
        for _ in range(50):
            await anext(archive)
            if open_response.await_count:
                break
        assert open_response.await_count == 1
        await archive.aclose()

    response.aclose.assert_awaited()
    assert chunk_sizes == [64 * 1024]


@pytest.mark.anyio
async def test_archive_emits_output_before_requesting_all_source_bytes() -> None:
    asset = _download_asset(uuid4(), size=1_000_004)
    response = Mock()
    response.aclose = AsyncMock()
    first_chunk_provided = asyncio.Event()
    second_chunk_requested = asyncio.Event()
    allow_second_chunk = asyncio.Event()

    async def aiter_bytes(chunk_size: int | None = None) -> AsyncIterator[bytes]:
        first_chunk_provided.set()
        yield b"x" * 1_000_000
        second_chunk_requested.set()
        await allow_second_chunk.wait()
        yield b"rest"

    response.aiter_bytes = aiter_bytes
    archive = _stream_archive([_compact_archive_asset(asset)])

    with patch(
        "routers.api.download.open_cdn_response",
        new_callable=AsyncMock,
        return_value=response,
    ):
        for _ in range(50):
            output = await asyncio.wait_for(anext(archive), timeout=1)
            if first_chunk_provided.is_set():
                break
        assert first_chunk_provided.is_set()
        assert output
        assert not second_chunk_requested.is_set()
        allow_second_chunk.set()
        await archive.aclose()

    response.aclose.assert_awaited()


@pytest.mark.anyio
async def test_archive_propagates_cdn_read_failure_and_closes_response() -> None:
    asset = _download_asset(uuid4(), size=10)
    response = Mock()
    response.aclose = AsyncMock()

    async def aiter_bytes(chunk_size: int | None = None) -> AsyncIterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("CDN connection lost")

    response.aiter_bytes = aiter_bytes
    archive = _stream_archive([_compact_archive_asset(asset)])

    with patch(
        "routers.api.download.open_cdn_response",
        new_callable=AsyncMock,
        return_value=response,
    ):
        with pytest.raises(httpx.ReadError, match="CDN connection lost"):
            _ = [chunk async for chunk in archive]

    response.aclose.assert_awaited_once()


@pytest.mark.anyio
async def test_archive_zip_generation_uses_a_dedicated_executor() -> None:
    asset = _download_asset(uuid4(), size=1)
    loop = asyncio.get_running_loop()
    run_in_executor = loop.run_in_executor
    executors: list[object | None] = []

    def record_executor(executor: Any, function: Any, *args: Any):
        executors.append(executor)
        return run_in_executor(executor, function, *args)

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
        yield b"x"

    with (
        patch.object(loop, "run_in_executor", side_effect=record_executor),
        patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks),
    ):
        _ = [chunk async for chunk in _stream_archive([_compact_archive_asset(asset)])]

    assert executors
    assert all(executor is not None for executor in executors)


@pytest.mark.anyio
async def test_archive_validates_all_metadata_before_returning_response() -> None:
    asset_ids = [uuid4() for _ in range(GUMNUT_API_MAX_BULK_IDS + 1)]
    assets = [_download_asset(asset_id) for asset_id in asset_ids]
    client = Mock()
    client.assets.list = Mock(
        side_effect=[
            MockSyncCursorPage(assets[:GUMNUT_API_MAX_BULK_IDS]),
            MockSyncCursorPage(assets[GUMNUT_API_MAX_BULK_IDS:]),
        ]
    )

    response = await download_archive(
        DownloadArchiveDto(assetIds=asset_ids), client=client
    )

    assert client.assets.list.call_count == 2
    body = cast(AsyncGenerator[bytes], response.body_iterator)
    await body.aclose()


@pytest.mark.anyio
async def test_archive_rejects_missing_requested_asset_before_streaming() -> None:
    asset_ids = [uuid4(), uuid4()]
    client = Mock()
    client.assets.list = Mock(
        return_value=MockSyncCursorPage([_download_asset(asset_ids[0])])
    )

    with pytest.raises(HTTPException) as exc_info:
        await download_archive(DownloadArchiveDto(assetIds=asset_ids), client=client)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Not found or no asset.download access"


@pytest.mark.anyio
async def test_archive_rejects_missing_asset_in_later_backend_chunk() -> None:
    asset_ids = [uuid4() for _ in range(GUMNUT_API_MAX_BULK_IDS + 1)]
    first_chunk_assets = [
        _download_asset(asset_id) for asset_id in asset_ids[:GUMNUT_API_MAX_BULK_IDS]
    ]
    client = Mock()
    client.assets.list = Mock(
        side_effect=[
            MockSyncCursorPage(first_chunk_assets),
            MockSyncCursorPage([]),
        ]
    )

    with pytest.raises(HTTPException) as exc_info:
        await download_archive(DownloadArchiveDto(assetIds=asset_ids), client=client)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Not found or no asset.download access"
    assert [len(call.kwargs["ids"]) for call in client.assets.list.call_args_list] == [
        GUMNUT_API_MAX_BULK_IDS,
        1,
    ]


@pytest.mark.anyio
async def test_archive_rejects_unavailable_original_before_streaming() -> None:
    missing_file_data = _download_asset(uuid4())
    missing_file_data.file_data = None
    missing_urls = _download_asset(uuid4())
    missing_urls.asset_urls = None
    missing_original = _download_asset(uuid4())
    missing_original.asset_urls = {"thumbnail": Mock(url="https://cdn.example.com/x")}
    for unavailable in (missing_file_data, missing_urls, missing_original):
        client = Mock()
        client.assets.list = Mock(return_value=MockSyncCursorPage([unavailable]))

        with pytest.raises(HTTPException) as exc_info:
            await download_archive(
                DownloadArchiveDto(assetIds=[safe_uuid_from_asset_id(unavailable.id)]),
                client=client,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Not found or no asset.download access"


@pytest.mark.anyio
async def test_archive_clamps_timestamps_to_zip_range() -> None:
    early = _download_asset(uuid4(), filename="early.jpg")
    future = _download_asset(uuid4(), filename="future.jpg")
    too_late = _download_asset(uuid4(), filename="too-late.jpg")
    assert early.file_data is not None
    assert future.file_data is not None
    assert too_late.file_data is not None
    early.file_data.file_modified_at = datetime(1970, 1, 2, 3, 4, 6)
    future.file_data.file_modified_at = datetime(2040, 2, 3, 4, 5, 6)
    too_late.file_data.file_modified_at = datetime(2200, 1, 2, 3, 4, 6)
    assets = [early, future, too_late]

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
        yield b"content"

    with patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks):
        archive = b"".join(
            [
                chunk
                async for chunk in _stream_archive(
                    [_compact_archive_asset(asset) for asset in assets]
                )
            ]
        )

    with ZipFile(BytesIO(archive)) as zip_file:
        assert [entry.date_time for entry in zip_file.infolist()] == [
            (1980, 1, 1, 0, 0, 0),
            (2040, 2, 3, 4, 5, 6),
            (2107, 12, 31, 23, 59, 58),
        ]
