"""Tests for batch download planning and streamed ZIP archives."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from threading import Event
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
    _abatched,
    _ArchiveAsset,
    _archive_content_disposition,
    _assets_by_ids,
    _assets_for_info,
    _build_download_info,
    _deduplicated_archive_name,
    _get_archive_zip_executor,
    _stream_archive,
    close_archive_zip_executor,
    download_archive,
    get_download_info,
)
from routers.immich_models import DownloadArchiveDto, DownloadInfoDto
from routers.utils.concurrency import BULK_FANOUT_CONCURRENCY_LIMIT
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
    kind: str = "original",
) -> AssetResponse:
    asset = make_gumnut_asset(
        asset_id=uuid_to_gumnut_asset_id(asset_id),
        original_file_name=filename,
        kind=kind,
    )
    asset.file_data.file_size_bytes = size
    asset.asset_urls = {
        "original": Mock(url=url or f"https://cdn.example.com/{asset_id}")
    }
    return cast(AssetResponse, asset)


def _make_version(position: int, *, url: str | None = None, size: int = 10) -> Mock:
    """Build a mock asset-version row for the version-chain resolver."""
    version = Mock()
    version.id = f"asset_version_pos{position}"
    version.position = position
    version.kind = "original" if position == 0 else "edit"
    version.mime_type = "image/jpeg"
    version.file_size_bytes = size
    version_urls: dict[str, Mock] = {}
    if url is not None:
        version_urls["original"] = Mock(url=url, mimetype="image/jpeg")
    version.version_urls = version_urls
    return version


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


@pytest.mark.parametrize(
    ("archive_name", "expected"),
    [
        (None, 'attachment; filename="assets.zip"'),
        ("", 'attachment; filename="assets.zip"'),
        ("Trip 2024", "attachment; filename*=UTF-8''Trip%202024.zip"),
        ("café+1", "attachment; filename*=UTF-8''caf%C3%A9%2B1.zip"),
        # A CRLF injection attempt is percent-encoded, never a raw header break.
        ("evil\r\nX-Bad: 1", "attachment; filename*=UTF-8''evil%0D%0AX-Bad%3A%201.zip"),
    ],
)
def test_archive_content_disposition_honors_requested_name(
    archive_name: str | None, expected: str
) -> None:
    assert _archive_content_disposition(archive_name) == expected


@pytest.mark.anyio
async def test_archive_uses_requested_archive_name_in_content_disposition() -> None:
    asset_id = uuid4()
    client = Mock()
    client.assets.list = Mock(
        return_value=MockSyncCursorPage(
            [_download_asset(asset_id, url="https://cdn.example.com/only")]
        )
    )

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
        yield b"data"

    with patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks):
        response = await download_archive(
            DownloadArchiveDto(assetIds=[asset_id], archiveName="Family Trip"),
            client=client,
        )
        await _collect_response_body(response)

    assert response.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''Family%20Trip.zip"
    )


@pytest.mark.anyio
async def test_assets_by_ids_chunks_backend_filter_and_preserves_duplicates() -> None:
    asset_ids = [uuid4() for _ in range(GUMNUT_API_MAX_BULK_IDS + 1)]
    selected = _download_asset(asset_ids[0])
    client = Mock()
    pages = iter([MockSyncCursorPage([selected]), MockSyncCursorPage([selected])])
    conversion_counts: list[int] = []

    def list_assets(**kwargs: Any) -> MockSyncCursorPage:
        conversion_counts.append(convert.call_count)
        return next(pages)

    with patch(
        "routers.api.download.uuid_to_gumnut_asset_id",
        wraps=uuid_to_gumnut_asset_id,
    ) as convert:
        client.assets.list = Mock(side_effect=list_assets)
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
    assert conversion_counts == [GUMNUT_API_MAX_BULK_IDS, len(asset_ids) + 1]
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

    result = await _build_download_info(asset_stream(), 30_000, Mock())

    assert result.totalSize == 251_456
    assert [archive.size for archive in result.archives] == [105_000, 146_456]
    assert result.archives[0].assetIds == [
        safe_uuid_from_asset_id(assets[0].id),
        safe_uuid_from_asset_id(assets[1].id),
    ]


@pytest.mark.anyio
async def test_abatched_groups_stream_into_fixed_size_batches() -> None:
    async def stream(items: list[int]) -> AsyncIterator[int]:
        for item in items:
            yield item

    async def batches(items: list[int], size: int) -> list[list[int]]:
        return [batch async for batch in _abatched(stream(items), size)]

    # Empty stream yields nothing.
    assert await batches([], 3) == []
    # Exact multiple yields no trailing empty batch.
    assert await batches([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]
    # A short final batch is flushed.
    assert await batches([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


@pytest.mark.anyio
async def test_download_info_bounds_edited_size_fanout_and_groups_across_waves() -> (
    None
):
    # More edited members than the fan-out limit, so /info resolves their
    # original sizes in bounded waves. Peak concurrency must stay within the
    # limit (an unbounded gather would exceed it), and threshold grouping must
    # stay request-exact even when the crossing lands mid-second-wave and the
    # version-chain fetches complete in reverse request order.
    member_count = BULK_FANOUT_CONCURRENCY_LIMIT * 2
    asset_ids = [uuid4() for _ in range(member_count)]
    # Each member's current rendering is 999 bytes, but /info must report the
    # position-0 upload. Uploads shrink with index (20, 19, ... 1) so the whole
    # heavy head lands in one archive and the light tail in the next — and so a
    # mis-ordered result would shift the crossing and be visible.
    assets = [
        _download_asset(asset_id, size=999, kind="edit") for asset_id in asset_ids
    ]
    upload_sizes = {
        uuid_to_gumnut_asset_id(asset_id): member_count - index
        for index, asset_id in enumerate(asset_ids)
    }

    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def versions_list(gumnut_asset_id: str, **kwargs: Any) -> list[Mock]:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        # Larger (earlier) members sleep longest, so completion order reverses
        # request order — ordered consumption must still place each size right.
        size = upload_sizes[gumnut_asset_id]
        await asyncio.sleep(size * 0.002)
        async with lock:
            active -= 1
        return [_make_version(0, size=size), _make_version(1, size=999)]

    client = Mock()
    client.assets.versions.list = AsyncMock(side_effect=versions_list)

    async def asset_stream() -> AsyncIterator[AssetResponse]:
        for asset in assets:
            yield asset

    # Uploads 20..1 sum to 210. A 175-byte target crosses after 13 members
    # (cumulative 182 for sizes 20..8), i.e. inside the second wave (members
    # 10..19); the 28-byte tail (sizes 7..1) then never crosses again.
    result = await _build_download_info(asset_stream(), 175, client)

    assert peak > 1, "expected concurrent edited-size resolution"
    assert peak <= BULK_FANOUT_CONCURRENCY_LIMIT
    assert client.assets.versions.list.await_count == member_count
    # /info reads only file_size_bytes, so it must not sign version URLs.
    assert all(
        "include" not in call.kwargs
        for call in client.assets.versions.list.await_args_list
    )
    expected_ids = [safe_uuid_from_asset_id(asset.id) for asset in assets]
    assert [archive.assetIds for archive in result.archives] == [
        expected_ids[:13],
        expected_ids[13:],
    ]
    assert [archive.size for archive in result.archives] == [182, 28]
    assert result.totalSize == 210


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


def test_archive_names_reserve_component_bytes_for_duplicate_suffixes() -> None:
    filename = f"{'a' * 251}.jpg"

    first, duplicate = _archive_names([filename, filename])

    assert first == filename
    assert duplicate.endswith("+1.jpg")
    assert len(first.encode("utf-8")) == 255
    assert len(duplicate.encode("utf-8")) == 255


def test_archive_names_truncate_multibyte_components_on_codepoint_boundaries() -> None:
    first, duplicate = _archive_names([f"{'é' * 200}.jpg"] * 2)

    assert first.endswith(".jpg")
    assert duplicate.endswith("+1.jpg")
    assert len(first.encode("utf-8")) <= 255
    assert len(duplicate.encode("utf-8")) <= 255


def test_archive_names_revalidate_windows_devices_after_truncation() -> None:
    filename = f"CONextra.{'a' * 251}"

    assert _archive_names([filename]) == ["unnamed"]


@pytest.mark.parametrize(
    "filename",
    ["COM¹.jpg", "COM².jpg", "COM³.jpg", "LPT¹.jpg", "LPT².jpg", "LPT³.jpg"],
)
def test_archive_names_reject_superscript_windows_devices(filename: str) -> None:
    assert _archive_names([filename]) == ["unnamed"]


def test_distinct_truncated_names_share_suffix_progression() -> None:
    next_suffix: dict[str, int] = {}
    emitted: set[str] = set()
    filenames = [f"{'a' * 255}{index}" for index in range(4)]

    names = [
        _deduplicated_archive_name(filename, next_suffix, emitted)
        for filename in filenames
    ]

    assert names == [
        f"{'a' * 255}",
        f"{'a' * 253}+1",
        f"{'a' * 253}+2",
        f"{'a' * 253}+3",
    ]
    assert len(next_suffix) == 1
    assert list(next_suffix.values()) == [4]


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
async def test_info_rejects_unavailable_file_size() -> None:
    missing_file_data = _download_asset(uuid4())
    missing_file_data.file_data = None
    missing_size = _download_asset(uuid4())
    assert missing_size.file_data is not None
    cast(Any, missing_size.file_data).file_size_bytes = None

    for unavailable in (missing_file_data, missing_size):
        client = Mock()
        client.assets.list = Mock(return_value=MockSyncCursorPage([unavailable]))

        with pytest.raises(HTTPException) as exc_info:
            await get_download_info(
                DownloadInfoDto(assetIds=[safe_uuid_from_asset_id(unavailable.id)]),
                client=client,
            )

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
async def test_cancelling_archive_read_unwinds_pending_cdn_open() -> None:
    asset = _download_asset(uuid4(), size=10)
    open_started = asyncio.Event()
    open_cancelled = asyncio.Event()

    async def blocked_open(url: str) -> httpx.Response:
        open_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            open_cancelled.set()
            raise
        raise AssertionError("unreachable")

    archive = _stream_archive([_compact_archive_asset(asset)])
    pending_read: asyncio.Task[bytes] | None = None

    with patch("routers.api.download.open_cdn_response", side_effect=blocked_open):
        async with asyncio.timeout(2):
            for _ in range(50):
                next_chunk = asyncio.create_task(anext(archive))
                open_wait = asyncio.create_task(open_started.wait())
                completed, _ = await asyncio.wait(
                    {next_chunk, open_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if open_wait in completed:
                    assert not next_chunk.done()
                    pending_read = next_chunk
                    break
                await next_chunk
                open_wait.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await open_wait

        assert pending_read is not None
        pending_read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending_read, timeout=1)

        assert open_cancelled.is_set()
        await archive.aclose()


@pytest.mark.anyio
async def test_cancelling_queued_archive_read_does_not_wait_for_worker() -> None:
    asset = _download_asset(uuid4(), size=10)
    worker_started = Event()
    release_worker = Event()
    executor = ThreadPoolExecutor(max_workers=1)

    def occupy_worker() -> None:
        worker_started.set()
        release_worker.wait()

    blocker = executor.submit(occupy_worker)
    assert await asyncio.to_thread(worker_started.wait, 1)
    archive_submitted = asyncio.Event()
    original_submit = executor.submit

    def record_submit(function: Any, *args: Any, **kwargs: Any):
        future = original_submit(function, *args, **kwargs)
        archive_submitted.set()
        return future

    archive = _stream_archive([_compact_archive_asset(asset)])
    try:
        with (
            patch(
                "routers.api.download._get_archive_zip_executor", return_value=executor
            ),
            patch.object(executor, "submit", side_effect=record_submit),
        ):
            pending_read = asyncio.create_task(anext(archive))
            await asyncio.wait_for(archive_submitted.wait(), timeout=1)
            pending_read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending_read, timeout=1)
            await archive.aclose()
    finally:
        release_worker.set()
        await asyncio.to_thread(blocker.result, 1)
        executor.shutdown(wait=True)


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
    executor = _get_archive_zip_executor()

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
        yield b"x"

    with (
        patch.object(executor, "submit", wraps=executor.submit) as submit,
        patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks),
    ):
        _ = [chunk async for chunk in _stream_archive([_compact_archive_asset(asset)])]

    assert submit.call_count > 0


@pytest.mark.anyio
async def test_archive_executor_shutdown_resets_and_is_idempotent() -> None:
    executor = _get_archive_zip_executor()

    await close_archive_zip_executor()

    with pytest.raises(RuntimeError, match="cannot schedule new futures"):
        executor.submit(lambda: None)
    replacement = _get_archive_zip_executor()
    assert replacement is not executor

    await close_archive_zip_executor()
    await close_archive_zip_executor()


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
    missing_size = _download_asset(uuid4())
    assert missing_size.file_data is not None
    cast(Any, missing_size.file_data).file_size_bytes = None
    missing_urls = _download_asset(uuid4())
    missing_urls.asset_urls = None
    missing_original = _download_asset(uuid4())
    missing_original.asset_urls = {"thumbnail": Mock(url="https://cdn.example.com/x")}
    for unavailable in (
        missing_file_data,
        missing_size,
        missing_urls,
        missing_original,
    ):
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
async def test_archive_resolves_nullable_file_modified_at_before_streaming() -> None:
    asset_id = uuid4()
    asset = _download_asset(asset_id, size=7)
    assert asset.file_data is not None
    cast(Any, asset.file_data).file_modified_at = None
    asset.metadata = Mock(modified_datetime=datetime(2024, 5, 6, 7, 8, 10))
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
        yield b"content"

    with patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks):
        response = await download_archive(
            DownloadArchiveDto(assetIds=[asset_id]), client=client
        )
        archive = await _collect_response_body(response)

    assert client.assets.list.call_args.kwargs["include"] == [
        "file_data",
        "metadata",
        "variants",
    ]
    with ZipFile(BytesIO(archive)) as zip_file:
        assert zip_file.infolist()[0].date_time == (2024, 5, 6, 7, 8, 10)


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


# --- Honoring `edited` on the batch download routes -------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("edited", "expected_url", "expected_size", "expected_bytes"),
    [
        (False, "https://cdn.example.com/upload", 10, b"u" * 10),
        (True, "https://cdn.example.com/edited", 20, b"e" * 20),
    ],
)
async def test_archive_honors_edited_for_edited_asset(
    edited: bool, expected_url: str, expected_size: int, expected_bytes: bytes
) -> None:
    asset_id = uuid4()
    # The current rendering (assets.list payload) is the edited file.
    asset = _download_asset(
        asset_id, size=20, url="https://cdn.example.com/edited", kind="edit"
    )
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))
    client.assets.versions.list = AsyncMock(
        return_value=[
            _make_version(0, url="https://cdn.example.com/upload", size=10),
            _make_version(1, url="https://cdn.example.com/edited", size=20),
        ]
    )
    payloads = {
        "https://cdn.example.com/upload": b"u" * 10,
        "https://cdn.example.com/edited": b"e" * 20,
    }
    requested: list[str] = []

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
        requested.append(url)
        yield payloads[url]

    with patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks):
        response = await download_archive(
            DownloadArchiveDto(assetIds=[asset_id], edited=edited), client=client
        )
        archive = await _collect_response_body(response)

    assert requested == [expected_url]
    with ZipFile(BytesIO(archive)) as zip_file:
        assert zip_file.namelist() == ["photo.jpg"]
        assert zip_file.read("photo.jpg") == expected_bytes
        assert zip_file.getinfo("photo.jpg").file_size == expected_size

    if edited:
        # The current rendering is served directly; no version-chain fetch.
        client.assets.versions.list.assert_not_called()
    else:
        client.assets.versions.list.assert_awaited_once()
        await_args = client.assets.versions.list.await_args
        assert await_args is not None
        assert await_args.kwargs["include"] == ["variants"]


@pytest.mark.anyio
async def test_archive_defaults_to_original_when_edited_omitted() -> None:
    asset_id = uuid4()
    asset = _download_asset(
        asset_id, size=20, url="https://cdn.example.com/edited", kind="edit"
    )
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))
    client.assets.versions.list = AsyncMock(
        return_value=[
            _make_version(0, url="https://cdn.example.com/upload", size=10),
            _make_version(1, url="https://cdn.example.com/edited", size=20),
        ]
    )

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
        assert url == "https://cdn.example.com/upload"
        yield b"u" * 10

    with patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks):
        response = await download_archive(
            DownloadArchiveDto(assetIds=[asset_id]), client=client
        )
        archive = await _collect_response_body(response)

    client.assets.versions.list.assert_awaited_once()
    with ZipFile(BytesIO(archive)) as zip_file:
        assert zip_file.read("photo.jpg") == b"u" * 10


@pytest.mark.anyio
@pytest.mark.parametrize("edited", [False, True])
async def test_archive_root_only_asset_is_identical_for_both_edited_values(
    edited: bool,
) -> None:
    asset_id = uuid4()
    asset = _download_asset(
        asset_id, size=7, url="https://cdn.example.com/root", kind="original"
    )
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))
    client.assets.versions.list = AsyncMock()

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
        assert url == "https://cdn.example.com/root"
        yield b"content"

    with patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks):
        response = await download_archive(
            DownloadArchiveDto(assetIds=[asset_id], edited=edited), client=client
        )
        archive = await _collect_response_body(response)

    # A root-only asset's current rendering is the upload, so no chain fetch.
    client.assets.versions.list.assert_not_called()
    with ZipFile(BytesIO(archive)) as zip_file:
        assert zip_file.read("photo.jpg") == b"content"
        assert zip_file.getinfo("photo.jpg").file_size == 7


@pytest.mark.anyio
async def test_archive_invalid_chain_for_edited_asset_returns_502() -> None:
    asset_id = uuid4()
    asset = _download_asset(asset_id, kind="edit")
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))
    client.assets.versions.list = AsyncMock(return_value=[])  # no unique root

    with pytest.raises(HTTPException) as exc_info:
        await download_archive(
            DownloadArchiveDto(assetIds=[asset_id], edited=False), client=client
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Asset version chain is invalid"


@pytest.mark.anyio
async def test_archive_edited_asset_missing_root_original_returns_400() -> None:
    asset_id = uuid4()
    asset = _download_asset(asset_id, kind="edit")
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))
    client.assets.versions.list = AsyncMock(
        return_value=[_make_version(0, url=None), _make_version(1)]
    )

    with pytest.raises(HTTPException) as exc_info:
        await download_archive(
            DownloadArchiveDto(assetIds=[asset_id], edited=False), client=client
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Not found or no asset.download access"


@pytest.mark.anyio
async def test_info_reports_original_size_for_edited_asset() -> None:
    asset_id = uuid4()
    # Current rendering is 20 bytes; the position-0 upload is 10.
    asset = _download_asset(asset_id, size=20, kind="edit")
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))
    client.assets.versions.list = AsyncMock(
        return_value=[
            _make_version(0, url="https://cdn.example.com/upload", size=10),
            _make_version(1, size=20),
        ]
    )

    result = await get_download_info(
        DownloadInfoDto(assetIds=[asset_id]), client=client
    )

    assert result.totalSize == 10
    assert [archive.size for archive in result.archives] == [10]
    client.assets.versions.list.assert_awaited_once()
    # /info reads only file_size_bytes, so it must not sign version URLs.
    await_args = client.assets.versions.list.await_args
    assert await_args is not None
    assert "include" not in await_args.kwargs


@pytest.mark.anyio
async def test_info_root_only_asset_skips_version_chain() -> None:
    asset_id = uuid4()
    asset = _download_asset(asset_id, size=7, kind="original")
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))
    client.assets.versions.list = AsyncMock()

    result = await get_download_info(
        DownloadInfoDto(assetIds=[asset_id]), client=client
    )

    assert result.totalSize == 7
    client.assets.versions.list.assert_not_called()


@pytest.mark.anyio
async def test_info_invalid_chain_for_edited_asset_returns_502() -> None:
    asset_id = uuid4()
    asset = _download_asset(asset_id, kind="edit")
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage([asset]))
    client.assets.versions.list = AsyncMock(return_value=[])  # no unique root

    with pytest.raises(HTTPException) as exc_info:
        await get_download_info(DownloadInfoDto(assetIds=[asset_id]), client=client)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Asset version chain is invalid"


@pytest.mark.anyio
async def test_archive_preserves_request_order_across_mixed_edited_members() -> None:
    # An edited member's root resolves through an async version-chain fetch; a
    # root-only member resolves inline. The ZIP must follow request order even
    # when the earlier edited member's fetch finishes last.
    edited_a, root_b, edited_c = uuid4(), uuid4(), uuid4()
    assets = [
        _download_asset(edited_a, filename="a.jpg", size=20, kind="edit"),
        _download_asset(root_b, filename="b.jpg", size=7, kind="original"),
        _download_asset(edited_c, filename="c.jpg", size=30, kind="edit"),
    ]
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage(assets))

    roots = {
        uuid_to_gumnut_asset_id(edited_a): [
            _make_version(0, url="https://cdn.example.com/upload-a", size=10),
            _make_version(1, size=20),
        ],
        uuid_to_gumnut_asset_id(edited_c): [
            _make_version(0, url="https://cdn.example.com/upload-c", size=15),
            _make_version(1, size=30),
        ],
    }

    async def versions_list(gumnut_asset_id: str, **kwargs: Any) -> list[Mock]:
        # Delay the first edited member so it completes after the later one;
        # order preservation must not depend on completion order.
        if gumnut_asset_id == uuid_to_gumnut_asset_id(edited_a):
            await asyncio.sleep(0.05)
        return roots[gumnut_asset_id]

    client.assets.versions.list = AsyncMock(side_effect=versions_list)

    payloads = {
        "https://cdn.example.com/upload-a": b"a" * 10,
        f"https://cdn.example.com/{root_b}": b"b" * 7,
        "https://cdn.example.com/upload-c": b"c" * 15,
    }

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
        yield payloads[url]

    with patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks):
        response = await download_archive(
            DownloadArchiveDto(assetIds=[edited_a, root_b, edited_c], edited=False),
            client=client,
        )
        archive = await _collect_response_body(response)

    with ZipFile(BytesIO(archive)) as zip_file:
        assert zip_file.namelist() == ["a.jpg", "b.jpg", "c.jpg"]
        assert zip_file.read("a.jpg") == b"a" * 10
        assert zip_file.read("b.jpg") == b"b" * 7
        assert zip_file.read("c.jpg") == b"c" * 15
        assert [info.file_size for info in zip_file.infolist()] == [10, 7, 15]

    # Root-only member takes no version-chain fetch.
    assert client.assets.versions.list.await_count == 2


@pytest.mark.anyio
async def test_archive_bounds_edited_root_fanout_within_concurrency_limit() -> None:
    # More edited members than the fan-out limit, so preflight must resolve their
    # roots in bounded waves. Replacing the bounded helper with an unbounded
    # `asyncio.gather` over every member would drive peak concurrency to the
    # member count; the bound keeps it at or below BULK_FANOUT_CONCURRENCY_LIMIT
    # while still resolving more than one root at a time.
    member_count = BULK_FANOUT_CONCURRENCY_LIMIT * 2
    asset_ids = [uuid4() for _ in range(member_count)]
    assets = [
        _download_asset(asset_id, filename=f"edited-{index}.jpg", kind="edit")
        for index, asset_id in enumerate(asset_ids)
    ]
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage(assets))

    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def versions_list(gumnut_asset_id: str, **kwargs: Any) -> list[Mock]:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1
        return [
            _make_version(0, url=f"https://cdn.example.com/{gumnut_asset_id}", size=5),
            _make_version(1, size=9),
        ]

    client.assets.versions.list = AsyncMock(side_effect=versions_list)

    async def fake_cdn_chunks(url: str, state: object) -> AsyncIterator[bytes]:
        yield b"x" * 5

    with patch("routers.api.download._cdn_asset_chunks", side_effect=fake_cdn_chunks):
        response = await download_archive(
            DownloadArchiveDto(assetIds=asset_ids, edited=False), client=client
        )
        archive = await _collect_response_body(response)

    assert peak > 1, "expected concurrent edited-root resolution"
    assert peak <= BULK_FANOUT_CONCURRENCY_LIMIT
    assert client.assets.versions.list.await_count == member_count
    with ZipFile(BytesIO(archive)) as zip_file:
        assert zip_file.namelist() == [
            f"edited-{index}.jpg" for index in range(member_count)
        ]


@pytest.mark.anyio
async def test_archive_cancels_pending_members_when_one_preflight_fails() -> None:
    # One member's chain is invalid, so the response (a 502) is already
    # determined. The remaining members' root lookups must be cancelled rather
    # than left running to completion, so an aborted large archive stops issuing
    # upstream requests whose results would be discarded.
    failing_id = uuid4()
    pending_ids = [uuid4() for _ in range(3)]
    asset_ids = [failing_id, *pending_ids]
    assets = [
        _download_asset(asset_id, filename=f"m-{index}.jpg", kind="edit")
        for index, asset_id in enumerate(asset_ids)
    ]
    client = Mock()
    client.assets.list = Mock(return_value=MockSyncCursorPage(assets))

    started: list[str] = []
    cancelled: list[str] = []
    completed: list[str] = []

    async def versions_list(gumnut_asset_id: str, **kwargs: Any) -> list[Mock]:
        if gumnut_asset_id == uuid_to_gumnut_asset_id(failing_id):
            # Yield first so the sibling lookups are actually in flight when this
            # one fails; cancel_on_error must then cancel those in-flight
            # requests rather than await their now-useless results.
            await asyncio.sleep(0.02)
            return []  # no unique root -> invalid chain -> 502
        started.append(gumnut_asset_id)
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(gumnut_asset_id)
            raise
        completed.append(gumnut_asset_id)  # pragma: no cover - sibling is cancelled
        return [
            _make_version(0, url=f"https://cdn.example.com/{gumnut_asset_id}"),
            _make_version(1),
        ]

    client.assets.versions.list = AsyncMock(side_effect=versions_list)

    with pytest.raises(HTTPException) as exc_info:
        await download_archive(
            DownloadArchiveDto(assetIds=asset_ids, edited=False), client=client
        )

    assert exc_info.value.status_code == 502
    # Siblings began before the failure propagated, then were cancelled — none
    # were allowed to finish their now-useless upstream round-trip.
    assert started, "expected sibling lookups to begin before the failure"
    assert set(cancelled) == set(started)
    assert completed == []
