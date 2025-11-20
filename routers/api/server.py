from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter
from shortuuid import uuid

from config.settings import get_settings
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
    "oauth": True,
    "oauthAutoLaunch": True,
    "passwordLogin": True,
    "configFile": False,
    "email": False,
    "ocr": False,
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


def get_fake_about() -> dict:
    """Generate fake about data with dynamic version."""
    version = get_settings().immich_version
    version_str = f"v{version}"
    return {
        "version": version_str,
        "versionUrl": f"https://github.com/immich-app/immich/releases/tag/{version_str}",
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


def get_fake_version_history() -> list:
    """Generate fake version history with dynamic version."""
    version = get_settings().immich_version
    return [
        {
            "id": "b86ef90c-3973-4aae-8b74-2f24ac71fdd4",
            "createdAt": "2025-01-13T21:28:34.519+00:00",
            "version": str(version),
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
    return ServerAboutResponseDto(**get_fake_about())


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
        for item in get_fake_version_history()
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
    version = get_settings().immich_version
    version_str = f"v{version}"
    return ServerApkLinksDto(
        arm64v8a=f"https://github.com/immich-app/immich/releases/download/{version_str}/immich-{version_str}-arm64-v8a.apk",
        armeabiv7a=f"https://github.com/immich-app/immich/releases/download/{version_str}/immich-{version_str}-armeabi-v7a.apk",
        universal=f"https://github.com/immich-app/immich/releases/download/{version_str}/immich-{version_str}-universal.apk",
        x86_64=f"https://github.com/immich-app/immich/releases/download/{version_str}/immich-{version_str}-x86_64.apk",
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
    Returns the Immich version from .immich-container-tag file.
    """
    version = get_settings().immich_version
    return ServerVersionResponseDto(
        major=version.major,
        minor=version.minor,
        patch=version.patch,
    )


@router.get("/version-check")
async def get_version_check() -> VersionCheckStateResponseDto:
    """
    Check for version updates.
    Returns the current version from .immich-container-tag file.
    """
    version = get_settings().immich_version
    return VersionCheckStateResponseDto(
        checkedAt=str(datetime.now(timezone.utc)),
        releaseVersion=str(version),
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
