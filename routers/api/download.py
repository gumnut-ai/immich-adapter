"""Immich-compatible batch download planning and streaming ZIP archives."""

import posixpath
import re
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from itertools import batched
from stat import S_IFREG
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from gumnut import AsyncGumnut
from gumnut.types.asset_response import AssetResponse
from pydantic.json_schema import SkipJsonSchema
from stream_zip import ZIP_AUTO, AsyncMemberFile, async_stream_zip

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
_UNSAFE_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(slots=True)
class _ArchiveStreamState:
    """Per-response ownership of the currently open CDN connection."""

    response: httpx.Response | None = None


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


async def _validated_archive_assets(
    client: AsyncGumnut, asset_ids: list[UUID]
) -> list[AssetResponse]:
    """Resolve every requested asset before starting an archive response."""
    assets = [
        asset
        async for asset in _assets_by_ids(
            client, asset_ids, include=_DOWNLOAD_ARCHIVE_INCLUDE
        )
    ]
    requested_ids = {uuid_to_gumnut_asset_id(asset_id) for asset_id in asset_ids}
    resolved_ids = {asset.id for asset in assets}
    originals_available = all(
        asset.file_data is not None
        and asset.asset_urls is not None
        and "original" in asset.asset_urls
        for asset in assets
    )
    if requested_ids != resolved_ids or not originals_available:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not found or no asset.download access",
        )
    return assets


async def _iterate_assets(assets: list[AssetResponse]) -> AsyncIterator[AssetResponse]:
    for asset in assets:
        yield asset


async def _assets_for_info(
    request: DownloadInfoDto,
    client: AsyncGumnut,
) -> AsyncIterator[AssetResponse]:
    """Resolve the selector precedence used by Immich's download service."""
    if request.assetIds is not None:
        async for asset in _assets_by_ids(
            client, request.assetIds, include=_DOWNLOAD_INFO_INCLUDE
        ):
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
    sanitized = _UNSAFE_FILENAME_CHARACTERS.sub("", filename).strip().rstrip(".")
    reserved_windows_name = re.fullmatch(
        r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", sanitized
    )
    if sanitized in {"", ".", ".."} or reserved_windows_name:
        return "unnamed"
    return sanitized


def _deduplicated_archive_name(
    original_file_name: str,
    next_suffix: dict[str, int],
    emitted: set[str],
) -> str:
    """Return a globally unique member name and reserve it."""
    base = _sanitize_archive_filename(original_file_name)
    suffix = next_suffix.get(base, 0)
    stem, extension = posixpath.splitext(base)
    candidate = base if suffix == 0 else f"{stem}+{suffix}{extension}"
    while candidate in emitted:
        suffix += 1
        candidate = f"{stem}+{suffix}{extension}"
    next_suffix[base] = suffix + 1
    emitted.add(candidate)
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
    assets: AsyncIterable[AssetResponse],
    state: _ArchiveStreamState,
) -> AsyncIterator[AsyncMemberFile]:
    next_suffix: dict[str, int] = {}
    emitted_names: set[str] = set()
    async for asset in assets:
        asset_urls = asset.asset_urls
        file_data = asset.file_data
        if file_data is None or not asset_urls or "original" not in asset_urls:
            raise RuntimeError("archive asset was not validated before streaming")
        variant = asset_urls["original"]
        yield (
            _deduplicated_archive_name(
                asset.original_file_name, next_suffix, emitted_names
            ),
            _zip_modified_at(file_data.file_modified_at),
            S_IFREG | 0o600,
            ZIP_AUTO(file_data.file_size_bytes, level=0),
            _cdn_asset_chunks(variant.url, state),
        )


async def _stream_archive(
    assets: AsyncIterable[AssetResponse],
) -> AsyncGenerator[bytes]:
    """Yield ZIP bytes and close the active CDN response on cancellation."""
    state = _ArchiveStreamState()
    try:
        async for chunk in async_stream_zip(
            _archive_members(assets, state),
            extended_timestamps=False,
        ):
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
    """Stream requested original assets as an on-the-fly ZIP archive.

    Gumnut does not currently expose edited variants, so the accepted
    ``edited`` compatibility flag falls back to each asset's original.
    """
    assets = await _validated_archive_assets(client, request.assetIds)
    return StreamingResponse(
        _stream_archive(_iterate_assets(assets)),
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
