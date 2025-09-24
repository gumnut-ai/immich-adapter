from fastapi import APIRouter, Query, Response

from routers.immich_models import (
    AssetIdsDto,
    DownloadInfoDto,
    DownloadResponseDto,
)

router = APIRouter(
    prefix="/api/download",
    tags=["download"],
    responses={404: {"description": "Not found"}},
)


@router.post("/archive")
async def download_archive(
    request: AssetIdsDto,
    key: str = Query(default=None),
    slug: str = Query(default=None),
) -> Response:
    """
    Download an archive of assets.
    This is a stub implementation that returns empty binary data.
    """
    # Return fake binary content as a zip file
    fake_zip_content = b""

    return Response(
        content=fake_zip_content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename=assets.zip",
            "Content-Length": str(len(fake_zip_content)),
        },
    )


@router.post("/info", status_code=201)
async def get_download_info(
    request: DownloadInfoDto,
    key: str = Query(default=None),
    slug: str = Query(default=None),
) -> DownloadResponseDto:
    """
    Get download information.
    This is a stub implementation that returns fake download info.
    """
    return DownloadResponseDto(
        archives=[],
        totalSize=0,
    )
