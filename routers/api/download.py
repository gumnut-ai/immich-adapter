"""Immich-compatible batch download planning and streaming ZIP archives."""

import asyncio
import posixpath
import re
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime
from itertools import batched
from stat import S_IFREG
from threading import Event, Lock
from typing import Annotated, Any, TypeVar, cast
from unicodedata import normalize
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from gumnut import AsyncGumnut
from gumnut.types.asset_response import AssetResponse
from pydantic.json_schema import SkipJsonSchema
from stream_zip import ZIP_AUTO, AsyncMemberFile, stream_zip

from routers.api.constants import (
    DEFAULT_DOWNLOAD_ARCHIVE_SIZE,
    GUMNUT_API_MAX_BULK_IDS,
    GUMNUT_API_MAX_PAGE_SIZE,
)
from routers.immich_models import (
    DownloadArchiveDto,
    DownloadArchiveInfo,
    DownloadInfoDto,
    DownloadResponseDto,
)
from routers.utils.asset_conversion import resolve_file_modified_at
from routers.utils.cdn_client import iter_cdn_response_bytes, open_cdn_response
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    safe_uuid_from_user_id,
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
)

router = APIRouter(
    prefix="/api/download",
    tags=["download"],
    responses={404: {"description": "Not found"}},
)

_DOWNLOAD_INFO_INCLUDE = ["file_data"]
_DOWNLOAD_ARCHIVE_INCLUDE = ["file_data", "metadata", "variants"]
_ZIP_MIN_TIMESTAMP = datetime(1980, 1, 1)
_ZIP_MAX_TIMESTAMP = datetime(2107, 12, 31, 23, 59, 58)
_ARCHIVE_CDN_CHUNK_SIZE = 64 * 1024
_ARCHIVE_ZIP_MAX_WORKERS = 8
_MAX_ARCHIVE_COMPONENT_BYTES = 255
_UNSAFE_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_archive_zip_executor: ThreadPoolExecutor | None = None
_archive_zip_executor_lock = Lock()
_T = TypeVar("_T")


@dataclass(slots=True)
class _ArchiveStreamState:
    """Per-response ownership of the currently open CDN connection."""

    response: httpx.Response | None = None


@dataclass(frozen=True, slots=True)
class _ArchiveAsset:
    """Compact metadata retained after archive preflight validation."""

    filename: str
    modified_at: datetime
    size: int
    url: str


def _get_archive_zip_executor() -> ThreadPoolExecutor:
    """Return the bounded executor reserved for ZIP generation."""
    global _archive_zip_executor
    if _archive_zip_executor is None:
        with _archive_zip_executor_lock:
            if _archive_zip_executor is None:
                _archive_zip_executor = ThreadPoolExecutor(
                    max_workers=_ARCHIVE_ZIP_MAX_WORKERS,
                    thread_name_prefix="archive-zip",
                )
    return _archive_zip_executor


async def close_archive_zip_executor() -> None:
    """Shut down the archive executor during application teardown."""
    global _archive_zip_executor
    with _archive_zip_executor_lock:
        executor = _archive_zip_executor
        _archive_zip_executor = None
    if executor is not None:
        await asyncio.to_thread(
            executor.shutdown,
            wait=True,
            cancel_futures=True,
        )


async def _assets_by_ids(
    client: AsyncGumnut,
    asset_ids: list[UUID],
    *,
    include: list[str],
) -> AsyncIterator[AssetResponse]:
    """Yield asset metadata in request order, one backend-sized chunk at a time."""
    for asset_id_chunk in batched(asset_ids, GUMNUT_API_MAX_BULK_IDS):
        chunk = [uuid_to_gumnut_asset_id(asset_id) for asset_id in asset_id_chunk]
        assets_by_id: dict[str, AssetResponse] = {}
        async for asset in client.assets.list(
            ids=chunk,
            include=include,
            limit=GUMNUT_API_MAX_PAGE_SIZE,
        ):
            assets_by_id[asset.id] = asset
        for asset_id in chunk:
            if asset := assets_by_id.get(asset_id):
                yield asset


async def _validated_info_assets_by_ids(
    client: AsyncGumnut,
    asset_ids: list[UUID],
) -> list[AssetResponse]:
    """Resolve every requested asset before building download info."""
    assets = [
        asset
        async for asset in _assets_by_ids(
            client,
            asset_ids,
            include=_DOWNLOAD_INFO_INCLUDE,
        )
    ]
    if len(assets) != len(asset_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not found or no asset.download access",
        )
    return assets


async def _validated_archive_assets(
    client: AsyncGumnut,
    asset_ids: list[UUID],
) -> list[_ArchiveAsset]:
    """Preflight every archive member while retaining only streaming metadata."""
    assets: list[_ArchiveAsset] = []
    async for asset in _assets_by_ids(
        client,
        asset_ids,
        include=_DOWNLOAD_ARCHIVE_INCLUDE,
    ):
        file_data = asset.file_data
        asset_urls = asset.asset_urls
        if (
            file_data is None
            or file_data.file_size_bytes is None
            or not asset_urls
            or "original" not in asset_urls
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Not found or no asset.download access",
            )
        assets.append(
            _ArchiveAsset(
                filename=asset.original_file_name,
                modified_at=resolve_file_modified_at(asset),
                size=file_data.file_size_bytes,
                url=asset_urls["original"].url,
            )
        )

    if len(assets) != len(asset_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not found or no asset.download access",
        )
    return assets


async def _assets_for_info(
    request: DownloadInfoDto,
    client: AsyncGumnut,
) -> AsyncIterator[AssetResponse]:
    """Resolve the selector precedence used by Immich's download service."""
    if request.assetIds is not None:
        assets = await _validated_info_assets_by_ids(client, request.assetIds)
        for asset in assets:
            yield asset
        return
    elif request.albumId is not None:
        album_id = uuid_to_gumnut_album_id(request.albumId)
        # Preserve Immich's not-found behavior for an invalid/inaccessible album
        # instead of silently returning an empty successful download.
        await client.albums.retrieve(album_id)
        assets = client.assets.list(
            album_id=album_id,
            include=_DOWNLOAD_INFO_INCLUDE,
            limit=GUMNUT_API_MAX_PAGE_SIZE,
        )
    elif request.userId is not None:
        current_user = await client.users.me()
        current_user_id = safe_uuid_from_user_id(current_user.id)
        if request.userId != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot download another user's library",
            )
        assets = client.assets.list(
            include=_DOWNLOAD_INFO_INCLUDE,
            limit=GUMNUT_API_MAX_PAGE_SIZE,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="assetIds, albumId, or userId is required",
        )

    async for asset in assets:
        yield asset


def _asset_size(asset: AssetResponse) -> int:
    file_data = asset.file_data
    if file_data is None or file_data.file_size_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not found or no asset.download access",
        )
    return file_data.file_size_bytes


async def _build_download_info(
    assets: AsyncIterable[AssetResponse], target_size: int
) -> DownloadResponseDto:
    archives: list[DownloadArchiveInfo] = []
    archive_ids: list[UUID] = []
    archive_size = 0

    async for asset in assets:
        archive_ids.append(safe_uuid_from_asset_id(asset.id))
        archive_size += _asset_size(asset)
        # Match Immich: the asset that crosses the threshold remains in the
        # current archive, so one oversized asset naturally forms one archive.
        if archive_size > target_size:
            archives.append(
                DownloadArchiveInfo(assetIds=archive_ids, size=archive_size)
            )
            archive_ids = []
            archive_size = 0

    if archive_ids:
        archives.append(DownloadArchiveInfo(assetIds=archive_ids, size=archive_size))

    return DownloadResponseDto(
        archives=archives,
        totalSize=sum(archive.size for archive in archives),
    )


def _sanitize_archive_filename(filename: str) -> str:
    """Return a safe, flat ZIP member filename with no traversal semantics."""
    sanitized = _UNSAFE_FILENAME_CHARACTERS.sub("", filename).strip().rstrip(" .")

    if _is_unusable_archive_name(sanitized):
        return "unnamed"
    return sanitized


def _is_unusable_archive_name(filename: str) -> bool:
    """Return whether a filename is empty or aliases a Windows device."""
    reserved_windows_name = re.fullmatch(
        r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", filename
    )
    return filename in {"", ".", ".."} or reserved_windows_name is not None


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate text without splitting a UTF-8 code point."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _archive_name_candidate(base: str, suffix: int) -> str:
    """Build a ZIP member name within common filesystem component limits."""
    stem, extension = posixpath.splitext(base)
    suffix_text = "" if suffix == 0 else f"+{suffix}"
    suffix_size = len(suffix_text.encode("utf-8"))
    extension = _truncate_utf8(
        extension,
        max(0, _MAX_ARCHIVE_COMPONENT_BYTES - suffix_size),
    )
    stem = _truncate_utf8(
        stem,
        max(
            0,
            _MAX_ARCHIVE_COMPONENT_BYTES - suffix_size - len(extension.encode("utf-8")),
        ),
    )
    candidate = f"{stem}{suffix_text}{extension}".rstrip(" .")
    return "unnamed" if _is_unusable_archive_name(candidate) else candidate


def _deduplicated_archive_name(
    original_file_name: str,
    next_suffix: dict[str, int],
    emitted_keys: set[str],
) -> str:
    """Return a filesystem-equivalent unique member name and reserve it."""

    def name_key(name: str) -> str:
        return normalize("NFC", name).casefold()

    base = _sanitize_archive_filename(original_file_name)
    base_candidate = _archive_name_candidate(base, 0)
    base_key = name_key(base_candidate)
    suffix = next_suffix.get(base_key, 0)
    candidate = base_candidate if suffix == 0 else _archive_name_candidate(base, suffix)
    while name_key(candidate) in emitted_keys:
        suffix += 1
        candidate = _archive_name_candidate(base, suffix)
    next_suffix[base_key] = suffix + 1
    emitted_keys.add(name_key(candidate))
    return candidate


def _zip_modified_at(modified_at: datetime) -> datetime:
    """Clamp timestamps to the range representable by a DOS ZIP header."""
    if modified_at.year < _ZIP_MIN_TIMESTAMP.year:
        return _ZIP_MIN_TIMESTAMP.replace(tzinfo=modified_at.tzinfo)
    if modified_at.year > _ZIP_MAX_TIMESTAMP.year:
        return _ZIP_MAX_TIMESTAMP.replace(tzinfo=modified_at.tzinfo)
    return modified_at


async def _cdn_asset_chunks(
    url: str, state: _ArchiveStreamState
) -> AsyncGenerator[bytes]:
    response = await open_cdn_response(url)
    state.response = response
    body = iter_cdn_response_bytes(response, chunk_size=_ARCHIVE_CDN_CHUNK_SIZE)
    try:
        async for chunk in body:
            yield chunk
    finally:
        await body.aclose()
        if state.response is response:
            state.response = None


async def _archive_members(
    assets: Iterable[_ArchiveAsset],
    state: _ArchiveStreamState,
) -> AsyncIterator[AsyncMemberFile]:
    next_suffix: dict[str, int] = {}
    emitted_name_keys: set[str] = set()
    for asset in assets:
        yield (
            _deduplicated_archive_name(asset.filename, next_suffix, emitted_name_keys),
            _zip_modified_at(asset.modified_at),
            S_IFREG | 0o600,
            ZIP_AUTO(asset.size, level=0),
            _cdn_asset_chunks(asset.url, state),
        )


async def _async_archive_zip(
    files: AsyncIterable[AsyncMemberFile],
) -> AsyncIterator[bytes]:
    """Bridge stream-zip through the archive-only bounded executor.

    The synchronous ZIP worker blocks on event-loop reads. Shield its executor
    future so task cancellation can first cancel that read, then wait for a
    running worker to unwind before the caller closes the active CDN response.
    """
    loop = asyncio.get_running_loop()
    bridge_cancelled = Event()
    pending_lock = Lock()
    pending_read: Future[Any] | None = None

    def cancel_pending_read() -> None:
        bridge_cancelled.set()
        with pending_lock:
            future = pending_read
        if future is not None:
            future.cancel()

    def to_sync_iterable(async_iterable: AsyncIterable[_T]) -> Iterable[_T]:
        nonlocal pending_read
        async_iterator = async_iterable.__aiter__()

        async def get_next() -> _T:
            return await anext(async_iterator)

        while True:
            future = asyncio.run_coroutine_threadsafe(get_next(), loop)
            with pending_lock:
                pending_read = future
                cancelled = bridge_cancelled.is_set()
            if cancelled:
                future.cancel()
            try:
                yield future.result()
            except StopAsyncIteration:
                break
            finally:
                with pending_lock:
                    if pending_read is future:
                        pending_read = None

    sync_member_files = (
        member_file[0:4] + (to_sync_iterable(member_file[4]),)
        for member_file in to_sync_iterable(files)
    )
    chunks = iter(
        stream_zip(
            files=sync_member_files,
            extended_timestamps=False,
        )
    )
    done = object()
    executor = _get_archive_zip_executor()
    while True:
        context = copy_context()

        def next_chunk() -> bytes | object:
            return next(chunks, done)

        concurrent_call = executor.submit(context.run, next_chunk)
        executor_call = asyncio.wrap_future(concurrent_call, loop=loop)
        try:
            value = await asyncio.shield(executor_call)
        except BaseException:
            cancel_pending_read()
            if not concurrent_call.cancel():
                while not executor_call.done():
                    try:
                        await asyncio.shield(executor_call)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                if executor_call.done() and not executor_call.cancelled():
                    try:
                        executor_call.result()
                    except BaseException:
                        pass
            raise
        if value is done:
            break
        yield cast(bytes, value)


async def _stream_archive(
    assets: Iterable[_ArchiveAsset],
) -> AsyncGenerator[bytes]:
    """Yield ZIP bytes and close the active CDN response on cancellation."""
    state = _ArchiveStreamState()
    try:
        async for chunk in _async_archive_zip(_archive_members(assets, state)):
            yield chunk
    finally:
        if state.response is not None:
            await state.response.aclose()
            state.response = None


@router.post("/archive")
async def download_archive(
    request: DownloadArchiveDto,
    key: Annotated[str | SkipJsonSchema[None], Query()] = None,
    slug: Annotated[str | SkipJsonSchema[None], Query()] = None,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> StreamingResponse:
    """Stream requested uploads as an on-the-fly ZIP archive.

    Gumnut asset chains are currently root-only, so the accepted ``edited``
    compatibility flag has no effect: the current rendering is the upload.
    """
    assets = await _validated_archive_assets(client, request.assetIds)
    return StreamingResponse(
        _stream_archive(assets),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="assets.zip"'},
    )


@router.post("/info", status_code=status.HTTP_201_CREATED)
async def get_download_info(
    request: DownloadInfoDto,
    key: Annotated[str | SkipJsonSchema[None], Query()] = None,
    slug: Annotated[str | SkipJsonSchema[None], Query()] = None,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> DownloadResponseDto:
    """Return archive groups and original byte sizes for a batch download."""
    return await _build_download_info(
        _assets_for_info(request, client),
        request.archiveSize or DEFAULT_DOWNLOAD_ARCHIVE_SIZE,
    )
