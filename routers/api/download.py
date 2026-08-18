"""Immich-compatible batch download planning and streaming ZIP archives."""

import logging
import posixpath
import re
from collections.abc import AsyncIterator
from itertools import batched
from stat import S_IFREG
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from gumnut import AsyncGumnut
from gumnut.types.asset_response import AssetResponse
from pydantic.json_schema import SkipJsonSchema
from stream_zip import ZIP_AUTO, async_stream_zip

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

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/download",
    tags=["download"],
    responses={404: {"description": "Not found"}},
)

_DOWNLOAD_ASSET_INCLUDE = ["file_data", "variants"]
_UNSAFE_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _is_downloadable(asset: AssetResponse) -> bool:
    return bool(
        asset.file_data is not None
        and asset.asset_urls
        and "original" in asset.asset_urls
    )


async def _assets_by_ids(
    client: AsyncGumnut, asset_ids: list[UUID]
) -> list[AssetResponse]:
    """Fetch asset metadata in backend-sized chunks and restore request order."""
    gumnut_ids = [uuid_to_gumnut_asset_id(asset_id) for asset_id in asset_ids]
    assets_by_id: dict[str, AssetResponse] = {}
    for chunk in batched(gumnut_ids, GUMNUT_API_MAX_BULK_IDS):
        async for asset in client.assets.list(
            ids=chunk,
            include=_DOWNLOAD_ASSET_INCLUDE,
            limit=GUMNUT_API_MAX_PAGE_SIZE,
        ):
            assets_by_id[asset.id] = asset
    return [
        assets_by_id[asset_id] for asset_id in gumnut_ids if asset_id in assets_by_id
    ]


async def _assets_for_info(
    request: DownloadInfoDto,
    client: AsyncGumnut,
) -> list[AssetResponse]:
    """Resolve the selector precedence used by Immich's download service."""
    if request.assetIds is not None:
        assets = await _assets_by_ids(client, request.assetIds)
    elif request.albumId is not None:
        album_id = uuid_to_gumnut_album_id(request.albumId)
        # Preserve Immich's not-found behavior for an invalid/inaccessible album
        # instead of silently returning an empty successful download.
        await client.albums.retrieve(album_id)
        assets = [
            asset
            async for asset in client.assets.list(
                album_id=album_id,
                include=_DOWNLOAD_ASSET_INCLUDE,
                limit=GUMNUT_API_MAX_PAGE_SIZE,
            )
        ]
    elif request.userId is not None:
        current_user = await client.users.me()
        current_user_id = safe_uuid_from_user_id(current_user.id)
        if request.userId != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot download another user's library",
            )
        assets = [
            asset
            async for asset in client.assets.list(
                include=_DOWNLOAD_ASSET_INCLUDE,
                limit=GUMNUT_API_MAX_PAGE_SIZE,
            )
        ]
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="assetIds, albumId, or userId is required",
        )

    return [asset for asset in assets if _is_downloadable(asset)]


def _asset_size(asset: AssetResponse) -> int:
    return asset.file_data.file_size_bytes if asset.file_data is not None else 0


def _build_download_info(
    assets: list[AssetResponse], target_size: int
) -> DownloadResponseDto:
    archives: list[DownloadArchiveInfo] = []
    archive_ids: list[UUID] = []
    archive_size = 0

    for asset in assets:
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


def _deduplicated_archive_names(assets: list[AssetResponse]) -> list[str]:
    seen: dict[str, int] = {}
    names: list[str] = []
    for asset in assets:
        filename = _sanitize_archive_filename(asset.original_file_name)
        count = seen.get(filename, 0)
        seen[filename] = count + 1
        if count:
            stem, extension = posixpath.splitext(filename)
            filename = f"{stem}+{count}{extension}"
        names.append(filename)
    return names


async def _cdn_asset_chunks(url: str) -> AsyncIterator[bytes]:
    response = await open_cdn_response(url)
    async for chunk in iter_cdn_response_bytes(response):
        yield chunk


async def _archive_members(assets: list[AssetResponse]):
    names = _deduplicated_archive_names(assets)
    for asset, filename in zip(assets, names, strict=True):
        asset_urls = asset.asset_urls
        file_data = asset.file_data
        if file_data is None or not asset_urls or "original" not in asset_urls:
            # Direct archive requests can bypass /download/info. Match Immich's
            # missing-asset tolerance and omit an unavailable original.
            logger.warning(
                "Skipping unavailable original in download archive",
                extra={"asset_id": asset.id},
            )
            continue
        variant = asset_urls["original"]
        size = file_data.file_size_bytes
        modified_at = file_data.file_modified_at
        yield (
            filename,
            modified_at,
            S_IFREG | 0o600,
            ZIP_AUTO(size, level=0),
            _cdn_asset_chunks(variant.url),
        )


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
    requested_assets = await _assets_by_ids(client, request.assetIds)
    assets: list[AssetResponse] = []
    for asset in requested_assets:
        if _is_downloadable(asset):
            assets.append(asset)
        else:
            # Direct archive requests can bypass /download/info. Match Immich's
            # missing-asset tolerance and omit an unavailable original before
            # assigning duplicate-name suffixes to the remaining members.
            logger.warning(
                "Skipping unavailable original in download archive",
                extra={"asset_id": asset.id},
            )
    return StreamingResponse(
        async_stream_zip(_archive_members(assets)),
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
    assets = await _assets_for_info(request, client)
    return _build_download_info(
        assets, request.archiveSize or DEFAULT_DOWNLOAD_ARCHIVE_SIZE
    )
