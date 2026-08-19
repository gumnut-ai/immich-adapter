"""Immich-compatible batch download planning and streaming ZIP archives."""

import asyncio
import posixpath
import re
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime
from itertools import batched
from stat import S_IFREG
from threading import Lock
from typing import Annotated, TypeVar, cast
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
_DOWNLOAD_ARCHIVE_INCLUDE = ["file_data", "variants"]
_ZIP_MIN_TIMESTAMP = datetime(1980, 1, 1)
_ZIP_MAX_TIMESTAMP = datetime(2107, 12, 31, 23, 59, 58)
_ARCHIVE_CDN_CHUNK_SIZE = 64 * 1024
_ARCHIVE_ZIP_MAX_WORKERS = 8
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
    gumnut_ids = [uuid_to_gumnut_asset_id(asset_id) for asset_id in asset_ids]
    for chunk in batched(gumnut_ids, GUMNUT_API_MAX_BULK_IDS):
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
    """Resolve every requested asset and its download-info metadata."""
    assets = [
        asset
        async for asset in _assets_by_ids(
            client,
            asset_ids,
            include=_DOWNLOAD_INFO_INCLUDE,
        )
    ]
    requested_ids = {uuid_to_gumnut_asset_id(asset_id) for asset_id in asset_ids}
    resolved_ids = {asset.id for asset in assets}
    metadata_available = all(asset.file_data is not None for asset in assets)
    if requested_ids != resolved_ids or not metadata_available:
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
    resolved_ids: set[str] = set()
    async for asset in _assets_by_ids(
        client,
        asset_ids,
        include=_DOWNLOAD_ARCHIVE_INCLUDE,
    ):
        file_data = asset.file_data
        asset_urls = asset.asset_urls
        if file_data is None or not asset_urls or "original" not in asset_urls:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Not found or no asset.download access",
            )
        resolved_ids.add(asset.id)
        assets.append(
            _ArchiveAsset(
                filename=asset.original_file_name,
                modified_at=file_data.file_modified_at,
                size=file_data.file_size_bytes,
                url=asset_urls["original"].url,
            )
        )

    requested_ids = {uuid_to_gumnut_asset_id(asset_id) for asset_id in asset_ids}
    if requested_ids != resolved_ids:
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
    return asset.file_data.file_size_bytes if asset.file_data is not None else 0


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
    reserved_windows_name = re.fullmatch(
        r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", sanitized
    )
    if sanitized in {"", ".", ".."} or reserved_windows_name:
        return "unnamed"
    return sanitized


def _deduplicated_archive_name(
    original_file_name: str,
    next_suffix: dict[str, int],
    emitted_keys: set[str],
) -> str:
    """Return a case-insensitively unique member name and reserve it."""
    base = _sanitize_archive_filename(original_file_name)
    base_key = base.casefold()
    suffix = next_suffix.get(base_key, 0)
    stem, extension = posixpath.splitext(base)
    candidate = base if suffix == 0 else f"{stem}+{suffix}{extension}"
    while candidate.casefold() in emitted_keys:
        suffix += 1
        candidate = f"{stem}+{suffix}{extension}"
    next_suffix[base_key] = suffix + 1
    emitted_keys.add(candidate.casefold())
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
    """Bridge stream-zip through the archive-only bounded executor."""
    loop = asyncio.get_running_loop()

    def to_sync_iterable(async_iterable: AsyncIterable[_T]) -> Iterable[_T]:
        async_iterator = async_iterable.__aiter__()

        async def get_next() -> _T:
            return await anext(async_iterator)

        while True:
            try:
                yield asyncio.run_coroutine_threadsafe(get_next(), loop).result()
            except StopAsyncIteration:
                break

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

        value = await loop.run_in_executor(executor, context.run, next_chunk)
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
