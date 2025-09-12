from datetime import datetime
from fastapi import APIRouter

from routers.immich_models import (
    ServerFeaturesDto,
    ServerConfigDto,
    ServerAboutResponseDto,
    ServerStorageResponseDto,
    ServerVersionHistoryResponseDto,
    ServerMediaTypesResponseDto,
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
    "version": "v1.124.2",
    "versionUrl": "https://github.com/immich-app/immich/releases/tag/v1.124.2",
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
        "version": "1.124.2",
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
async def get_version_history() -> list[ServerVersionHistoryResponseDto]:
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
