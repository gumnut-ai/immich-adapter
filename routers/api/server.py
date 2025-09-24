from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter
from shortuuid import uuid

from routers.immich_models import (
    LicenseKeyDto,
    LicenseResponseDto,
    ServerApkLinksDto,
    ServerFeaturesDto,
    ServerConfigDto,
    ServerAboutResponseDto,
    ServerPingResponse,
    ServerStatsResponseDto,
    ServerStorageResponseDto,
    ServerThemeDto,
    ServerVersionHistoryResponseDto,
    ServerVersionResponseDto,
    ServerMediaTypesResponseDto,
    VersionCheckStateResponseDto,
)

router = APIRouter(
    prefix="/api/server",
    tags=["server"],
    responses={404: {"description": "Not found"}},
)


fake_features = {
    "smartSearch": True,
    "facialRecognition": True,
    "duplicateDetection": True,
    "map": True,
    "reverseGeocoding": True,
    "importFaces": False,
    "sidecar": True,
    "search": True,
    "trash": True,
    "oauth": False,
    "oauthAutoLaunch": False,
    "passwordLogin": True,
    "configFile": False,
    "email": False,
}

fake_config = {
    "loginPageMessage": "",
    "trashDays": 30,
    "userDeleteDelay": 7,
    "oauthButtonText": "Login with OAuth",
    "isInitialized": True,
    "isOnboarded": True,
    "externalDomain": "",
    "publicUsers": True,
    "mapDarkStyleUrl": "https://tiles.immich.cloud/v1/style/dark.json",
    "mapLightStyleUrl": "https://tiles.immich.cloud/v1/style/light.json",
}

fake_about = {
    "version": "v1.142.0",
    "versionUrl": "https://github.com/immich-app/immich/releases/tag/v1.142.0",
    "licensed": False,
    "nodejs": "v20.18.1",
    "exiftool": "13.00",
    "ffmpeg": "7.0.2-7",
    "libvips": "8.15.3",
    "imagemagick": "7.1.1-40",
}

fake_storage = {
    "diskSize": "14.6 TiB",
    "diskUse": "11.2 TiB",
    "diskAvailable": "3.3 TiB",
    "diskSizeRaw": 15998417567744,
    "diskUseRaw": 12362364284928,
    "diskAvailableRaw": 3636053282816,
    "diskUsagePercentage": 77.27,
}

fake_version_history = [
    {
        "id": "b86ef90c-3973-4aae-8b74-2f24ac71fdd4",
        "createdAt": "2025-01-13T21:28:34.519+00:00",
        "version": "1.142.0",
    }
]

fake_media_types = {
    "video": [
        ".3gp",
        ".3gpp",
        ".avi",
        ".flv",
        ".insv",
        ".m2t",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpe",
        ".mpeg",
        ".mpg",
        ".mts",
        ".vob",
        ".webm",
        ".wmv",
    ],
    "image": [
        ".3fr",
        ".ari",
        ".arw",
        ".cap",
        ".cin",
        ".cr2",
        ".cr3",
        ".crw",
        ".dcr",
        ".dng",
        ".erf",
        ".fff",
        ".iiq",
        ".k25",
        ".kdc",
        ".mrw",
        ".nef",
        ".nrw",
        ".orf",
        ".ori",
        ".pef",
        ".psd",
        ".raf",
        ".raw",
        ".rw2",
        ".rwl",
        ".sr2",
        ".srf",
        ".srw",
        ".x3f",
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".heif",
        ".hif",
        ".insp",
        ".jpe",
        ".jpeg",
        ".jpg",
        ".jxl",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    ],
    "sidecar": [".xmp"],
}


@router.get("/features")
async def get_features() -> ServerFeaturesDto:
    return ServerFeaturesDto(**fake_features)


@router.get("/config")
async def get_config() -> ServerConfigDto:
    return ServerConfigDto(**fake_config)


@router.get("/about")
async def get_about() -> ServerAboutResponseDto:
    return ServerAboutResponseDto(**fake_about)


@router.get("/storage")
async def get_storage() -> ServerStorageResponseDto:
    return ServerStorageResponseDto(**fake_storage)


@router.get("/version-history")
async def get_version_history() -> List[ServerVersionHistoryResponseDto]:
    return [
        ServerVersionHistoryResponseDto(
            id=item["id"],
            version=item["version"],
            createdAt=datetime.fromisoformat(item["createdAt"]),
        )
        for item in fake_version_history
    ]


@router.get("/media-types")
async def get_media_types() -> ServerMediaTypesResponseDto:
    return ServerMediaTypesResponseDto(**fake_media_types)


@router.get("/apk-links")
async def get_apk_links() -> ServerApkLinksDto:
    """
    Get APK links for the Immich mobile app.
    This is a stub implementation returning fake download links.
    """
    return ServerApkLinksDto(
        arm64v8a="https://github.com/immich-app/immich/releases/download/v1.142.0/immich-v1.142.0-arm64-v8a.apk",
        armeabiv7a="https://github.com/immich-app/immich/releases/download/v1.142.0/immich-v1.142.0-armeabi-v7a.apk",
        universal="https://github.com/immich-app/immich/releases/download/v1.142.0/immich-v1.142.0-universal.apk",
        x86_64="https://github.com/immich-app/immich/releases/download/v1.142.0/immich-v1.142.0-x86_64.apk",
    )


@router.get("/ping")
async def ping_server() -> ServerPingResponse:
    """
    Ping the server to check if it's alive.
    This is a stub implementation that always returns 'pong'.
    """
    return ServerPingResponse(res="pong")


@router.get("/statistics")
async def get_server_statistics() -> ServerStatsResponseDto:
    """
    Get server statistics including photo count and usage.
    This is a stub implementation returning fake statistics.
    """
    return ServerStatsResponseDto(
        photos=0, usage=0, usageByUser=[], usagePhotos=0, usageVideos=0, videos=0
    )


@router.get("/version")
async def get_server_version() -> ServerVersionResponseDto:
    """
    Get server version information.
    This is a stub implementation returning version 1.142.0.
    """
    return ServerVersionResponseDto(major=1, minor=142, patch=0)


@router.get("/version-check")
async def get_version_check() -> VersionCheckStateResponseDto:
    """
    Check for version updates.
    This is a stub implementation returning version 1.142.0.
    """
    return VersionCheckStateResponseDto(
        checkedAt=str(datetime.now(timezone.utc)),
        releaseVersion="1.142.0",
    )


@router.get("/license")
async def get_server_license() -> LicenseResponseDto:
    """
    Get server license information.
    This is a stub implementation returning basic license info.
    """
    return LicenseResponseDto(
        licenseKey="/IMSV-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA/",
        activationKey=str(uuid()),
        activatedAt=datetime(1900, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


@router.delete("/license", status_code=204)
async def delete_server_license():
    """
    Delete server license information.
    This is a stub implementation that does not perform any action.
    """
    return


@router.put("/license")
async def set_server_license(request: LicenseKeyDto) -> LicenseResponseDto:
    """
    Update server license information.
    This is a stub implementation that does not perform any action.
    """
    return LicenseResponseDto(
        licenseKey=request.licenseKey,
        activationKey=request.activationKey,
        activatedAt=datetime.now(timezone.utc),
    )


@router.get("/theme")
async def get_theme() -> ServerThemeDto:
    """
    Get server theme configuration.
    This is a stub implementation returning empty custom CSS.
    """
    return ServerThemeDto(customCss="")
