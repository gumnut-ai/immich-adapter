"""Shared type aliases for the sync package."""

from typing import TypeAlias

from gumnut.types.album_asset_response import AlbumAssetResponse
from gumnut.types.album_response import AlbumResponse
from gumnut.types.asset_response import AssetResponse
from gumnut.types.exif_response import ExifResponse
from gumnut.types.face_response import FaceResponse
from gumnut.types.person_response import PersonResponse

EntityType: TypeAlias = (
    AssetResponse
    | AlbumResponse
    | AlbumAssetResponse
    | PersonResponse
    | FaceResponse
    | ExifResponse
)
